import json
import logging

import telegramify_markdown

from aiogram import html
from aiogram.types import MessageEntity as TgEntity

from .db import StoredMessage
from . import gemini as gemini_client
from .openrouter import chat_completion as openrouter_chat_completion
from .prompts import SYSTEM_PROMPT, build_user_prompt


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
    model: str,
    gemini_api_key: str | None = None,
    gemini_model: str | None = None,
) -> str:
    user_prompt = build_user_prompt(messages, per_user)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    if gemini_api_key:
        content = await gemini_client.chat_completion(
            api_key=gemini_api_key,
            model=gemini_model or "gemini-2.5-flash",
            messages=messages,
            response_format={"type": "json_object"},
        )
    else:
        response = await openrouter_chat_completion(
            api_key=api_key,
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        content = response["choices"][0]["message"]["content"]
    data = json.loads(content)
    return _format_telegram(data)


async def ask_question(
    question: str,
    *,
    api_key: str,
    model: str,
    gemini_api_key: str | None = None,
    gemini_model_ask: str | None = None,
) -> str:
    messages = [{"role": "user", "content": question}]
    if gemini_api_key:
        content = await gemini_client.chat_completion(
            api_key=gemini_api_key,
            model=gemini_model_ask or "gemini-2.5-pro",
            messages=messages,
            grounding=True,
        )
    else:
        response = await openrouter_chat_completion(
            api_key=api_key,
            model=model,
            messages=messages,
        )
        content = response["choices"][0]["message"]["content"]
    text, entities = telegramify_markdown.convert(content)
    return _as_expandable_quote(text, entities)


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

    iq_lines = ["🧠 IQ-рейтинг:"]
    for index, user in enumerate(users_sorted, 1):
        name = html.quote((user.get("name") or "???").replace("@", "@​"))
        iq = user.get("iq", "???")
        comment = (user.get("comment") or "").strip()
        suffix = f" · {html.quote(comment)}" if comment else ""
        iq_lines.append(f"{index}. {name} — {iq}{suffix}")
    parts.append(html.expandable_blockquote("\n".join(iq_lines)))

    parts.append("\n#summary")
    return "\n\n".join(parts)


def _as_expandable_quote(text: str, entities: list) -> tuple[str, list[TgEntity]]:
    aio_entities = [TgEntity(**e.to_dict()) for e in entities]
    utf16_len = len(text.encode("utf-16-le")) // 2
    quote = TgEntity(type="expandable_blockquote", offset=0, length=utf16_len)
    return text, [quote] + aio_entities
