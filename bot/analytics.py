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
from .openai_client import chat_completion as openai_chat_completion
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

    if gemini_model:
        if not gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is required when GEMINI_MODEL_SUMMARY is set")
        logger.info("/summary target provider=gemini model=%s", gemini_model)
        chat_json = _gemini_json_completion(gemini_api_key, gemini_model)
    else:
        logger.info("/summary target provider=openai-compatible base_url=%s model=%s", base_url, model)
        chat_json = _openai_json_completion(api_key, base_url, model, timeout)

    summary_result, iq_result = await asyncio.gather(
        chat_json("summary", summary_messages),
        chat_json("iq", iq_messages),
        return_exceptions=True,
    )
    if isinstance(summary_result, Exception) and isinstance(iq_result, Exception):
        raise RuntimeError("Both summary LLM requests failed") from summary_result

    summary_data = _parse_llm_part("summary", summary_result)
    iq_data = _parse_llm_part("iq", iq_result)
    data = {
        "summary": summary_data.get("summary", ""),
        "users": iq_data.get("users", []),
        "summary_error": bool(isinstance(summary_result, Exception)),
        "iq_error": bool(isinstance(iq_result, Exception)),
    }
    return _format_telegram(data)


async def ask_question(
    question: str,
    *,
    chat_id: int,
    user_id: int,
    db_path: str,
    gemini_api_key: str,
    gemini_model_ask: str | None = None,
    images: list[InlineImage] | None = None,
) -> tuple[str, list]:
    history = await get_ask_history(db_path, chat_id, user_id)
    messages = [
        {"role": "system", "content": ASK_SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": question},
    ]
    ask_model = gemini_model_ask or "gemma-4-31b-it"
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
    if not content or not content.strip():
        raise RuntimeError("AI returned empty response")
    now = int(time.time())
    history_question = f"{question}\n[image attached]" if images else question
    await append_ask_history(db_path, chat_id, user_id, "user", history_question, now)
    await append_ask_history(db_path, chat_id, user_id, "assistant", content, now)
    text, entities = telegramify_markdown.convert(content)
    return _as_expandable_quote(text, entities)


def _parse_json_object(content: str) -> dict:
    s = (content or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    start = s.find("{")
    if start > 0:
        s = s[start:]
    obj, _ = json.JSONDecoder().raw_decode(s)
    return obj


def _parse_llm_part(label: str, result: str | BaseException) -> dict:
    if isinstance(result, Exception):
        logger.exception("%s request failed", label, exc_info=result)
        return {}
    try:
        return _parse_json_object(result)
    except Exception:
        logger.exception("%s response is not valid JSON:\n%s", label, result)
        return {}


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
