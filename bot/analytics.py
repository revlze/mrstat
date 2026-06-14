import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import telegramify_markdown

from aiogram import html
from aiogram.types import MessageEntity as TgEntity

import time

from .db import StoredMessage, get_ask_history, append_ask_history
from . import gemini as gemini_client
from .gemini import InlineImage
from .openai_client import (
    chat_completion as openai_chat_completion,
    vision_chat_completion as openai_vision_chat_completion,
)
from .prompts import (
    ASK_SYSTEM_PROMPT,
    IQ_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    build_iq_prompt,
    build_summary_prompt,
)


logger = logging.getLogger(__name__)


def aggregate_by_user(
    messages: list[StoredMessage], min_words: int
) -> dict[int, dict]:
    per_user: dict[int, dict] = {}
    for message in messages:
        info = per_user.setdefault(
            message.user_id,
            {"display_name": _display_name(message), "texts": [], "word_count": 0},
        )
        if message.username and not info["display_name"].startswith("@"):
            info["display_name"] = f"@{message.username}"
        info["texts"].append(message.text)
        info["word_count"] += len(message.text.split())
    return {uid: info for uid, info in per_user.items() if info["word_count"] >= min_words}


async def build_summary(
    messages: list[StoredMessage],
    per_user: dict[int, dict],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
    timezone: str,
    gemini_api_key: str | None = None,
    gemini_model: str | None = None,
) -> str:
    summary_messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": build_summary_prompt(messages, per_user, timezone)},
    ]
    iq_messages = [
        {"role": "system", "content": IQ_SYSTEM_PROMPT},
        {"role": "user", "content": build_iq_prompt(messages, per_user, timezone)},
    ]

    if gemini_model and gemini_api_key:
        logger.info("/summary target provider=gemini model=%s", gemini_model)
        chat_json = _gemini_json_completion(gemini_api_key, gemini_model)
    else:
        if gemini_model and not gemini_api_key:
            logger.warning(
                "/summary provider=gemini requested but GEMINI_API_KEY is missing; falling back to openai-compatible"
            )
        logger.info("/summary target provider=openai-compatible base_url=%s model=%s", base_url, model)
        chat_json = _openai_json_completion(api_key, base_url, model, timeout)

    summary_result, iq_result = await asyncio.gather(
        chat_json("summary", summary_messages),
        chat_json("iq", iq_messages),
        return_exceptions=True,
    )
    if isinstance(summary_result, Exception) and isinstance(iq_result, Exception):
        raise RuntimeError("Both summary LLM requests failed") from summary_result

    summary_data, summary_error = _parse_llm_part("summary", summary_result)
    iq_data, iq_error = _parse_llm_part("iq", iq_result)
    data = {
        "summary": summary_data.get("summary", ""),
        "users": iq_data.get("users", []),
        "summary_error": summary_error,
        "iq_error": iq_error,
    }
    return _format_telegram(data)


async def ask_question(
    question: str,
    *,
    chat_id: int,
    user_id: int,
    db_path: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
    gemini_api_key: str | None = None,
    gemini_model_ask: str | None = None,
    images: list[InlineImage] | None = None,
) -> tuple[str, list, str]:
    history = await get_ask_history(db_path, chat_id, user_id)
    messages = [
        {"role": "system", "content": ASK_SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": question},
    ]
    if gemini_api_key:
        ask_model = gemini_model_ask or "gemini-2.5-pro"
        logger.info(
            "/ask target provider=gemini model=%s grounding=true images=%d",
            ask_model,
            len(images or []),
        )
        content = await gemini_client.chat_completion(
            api_key=gemini_api_key,
            model=ask_model,
            messages=messages,
            grounding=True,
            images=images,
        )
    else:
        logger.info(
            "/ask target provider=openai-compatible base_url=%s model=%s grounding=false images=%d",
            base_url,
            model,
            len(images or []),
        )
        content = await openai_vision_chat_completion(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            images=images,
            timeout=timeout,
        )
    if not content or not content.strip():
        raise RuntimeError("AI returned empty response")
    now = int(time.time())
    history_question = f"{question}\n[image attached]" if images else question
    await append_ask_history(db_path, chat_id, user_id, "user", history_question, now)
    await append_ask_history(db_path, chat_id, user_id, "assistant", content, now)
    text, entities = telegramify_markdown.convert(content)
    text, entities = _as_expandable_quote(text, entities)
    return text, entities, content


def _strip_response_fence(content: str) -> str:
    s = (content or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _strip_json_response(content: str) -> str:
    s = _strip_response_fence(content)
    start = s.find("{")
    if start > 0:
        s = s[start:]
    return s


def _parse_json_object(content: str) -> dict:
    s = _strip_json_response(content)
    obj, _ = json.JSONDecoder().raw_decode(s)
    if not isinstance(obj, dict):
        raise ValueError("JSON response is not an object")
    return obj


def _parse_llm_part(label: str, result: str | BaseException) -> tuple[dict, bool]:
    if isinstance(result, Exception):
        logger.exception("%s request failed", label, exc_info=result)
        return {}, True
    try:
        return _parse_json_object(result), False
    except Exception:
        logger.exception("%s response is not valid JSON:\n%s", label, result)
        if label == "summary":
            recovered = _recover_summary_object(result)
            if recovered:
                logger.warning("%s response recovered from malformed JSON", label)
                return recovered, False
            text = _clean_raw_summary_text(result)
            if text:
                logger.warning("%s response used as plain text after JSON parse failed", label)
                return {"summary": text}, False
        return {}, True


def _recover_summary_object(content: str) -> dict:
    s = _strip_json_response(content)
    key_pos = s.find('"summary"')
    if key_pos < 0:
        return {}
    colon_pos = s.find(":", key_pos + len('"summary"'))
    if colon_pos < 0:
        return {}

    decoder = json.JSONDecoder()
    fragments: list[str] = []
    pos = colon_pos + 1

    while True:
        pos = _skip_json_whitespace(s, pos)
        if pos >= len(s):
            break
        try:
            value, end = decoder.raw_decode(s, pos)
        except json.JSONDecodeError:
            break
        next_pos = _skip_json_whitespace(s, end)
        if next_pos < len(s) and s[next_pos] == ":":
            break
        if not isinstance(value, str):
            break
        fragments.append(value)

        next_pos = _skip_json_whitespace(s, end)
        if next_pos >= len(s) or s[next_pos] != ",":
            break
        pos = next_pos + 1

    summary = "\n\n".join(fragment.strip() for fragment in fragments if fragment.strip())
    return {"summary": summary} if summary else {}


def _clean_raw_summary_text(content: str) -> str:
    return _strip_response_fence(content).strip()


def _skip_json_whitespace(s: str, pos: int) -> int:
    while pos < len(s) and s[pos] in " \t\r\n":
        pos += 1
    return pos


def _gemini_json_completion(
    api_key: str,
    model: str,
) -> Callable[[str, list[dict]], Awaitable[str]]:
    async def complete(label: str, messages: list[dict]) -> str:
        content = await gemini_client.chat_completion(
            api_key=api_key,
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        logger.info("%s raw response (%d chars):\n%s", label, len(content or ""), content)
        return content

    return complete


def _openai_json_completion(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
) -> Callable[[str, list[dict]], Awaitable[str]]:
    async def complete(label: str, messages: list[dict]) -> str:
        content = await openai_chat_completion(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        logger.info("%s raw response (%d chars):\n%s", label, len(content or ""), content)
        return content

    return complete


def _display_name(message: StoredMessage) -> str:
    if message.username:
        return f"@{message.username}"
    if message.full_name:
        return message.full_name
    return str(message.user_id)


def _format_telegram(data: dict) -> str:
    summary_text = (data.get("summary") or "").strip()
    users = data.get("users") or []
    users_sorted = sorted(users, key=lambda u: u.get("iq", 0), reverse=True)

    parts = ["📊 Саммари за сутки"]

    if summary_text:
        parts.append(html.expandable_blockquote(summary_text))
    elif data.get("summary_error"):
        parts.append(html.expandable_blockquote("Обзор временно не собрался: модель не ответила вовремя."))

    iq_lines = ["🧠 IQ-рейтинг:"]
    if users_sorted:
        for index, user in enumerate(users_sorted, 1):
            name = html.quote((user.get("name") or "???").replace("@", "@​"))
            iq = user.get("iq", "???")
            comment = (user.get("comment") or "").strip()
            suffix = f" · {html.quote(comment)}" if comment else ""
            iq_lines.append(f"{index}. {name} — {iq}{suffix}")
    elif data.get("iq_error"):
        iq_lines.append("Временно не собрался: модель не ответила вовремя.")
    parts.append(html.expandable_blockquote("\n".join(iq_lines)))

    parts.append("\n#summary")
    return "\n\n".join(parts)


def _as_expandable_quote(text: str, entities: list) -> tuple[str, list[TgEntity]]:
    aio_entities = [TgEntity(**e.to_dict()) for e in entities if e.type != "pre"]
    utf16_len = len(text.encode("utf-16-le")) // 2
    quote = TgEntity(type="expandable_blockquote", offset=0, length=utf16_len)
    return text, [quote] + aio_entities
