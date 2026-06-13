import asyncio
import logging
import random
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message, TelegramObject

from .analytics import aggregate_by_user, ask_question, build_summary
from .config import Config
from .db import delete_user_messages, get_last_summary, get_messages_for_period, get_summary_calls_today, increment_summary_calls, save_last_summary, save_message, update_message
from .logging_sink import chat_ref, log_to_chat


logger = logging.getLogger(__name__)


class AllowlistMiddleware(BaseMiddleware):
    def __init__(
        self,
        allowed_chat_ids: frozenset[int],
        allowed_user_ids: frozenset[int],
        blocked_user_ids: frozenset[int],
        sudo_user_ids: frozenset[int],
    ) -> None:
        self.allowed_chat_ids = allowed_chat_ids
        self.allowed_user_ids = allowed_user_ids
        self.blocked_user_ids = blocked_user_ids
        self.sudo_user_ids = sudo_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)
        if chat is None:
            return None
        if user is not None and user.id in self.blocked_user_ids:
            logger.debug("blocked event from blocklisted user %d", user.id)
            return None
        is_sudo = user is not None and user.id in self.sudo_user_ids
        if not is_sudo:
            if chat.type in ("group", "supergroup"):
                if self.allowed_chat_ids and chat.id not in self.allowed_chat_ids:
                    logger.debug("blocked event in non-allowed chat %d", chat.id)
                    return None
            elif chat.type == "private":
                if self.allowed_user_ids and (user is None or user.id not in self.allowed_user_ids):
                    logger.debug("blocked private event from user %s", user.id if user else None)
                    return None
            else:
                return None
        return await handler(event, data)

_summary_locks: dict[int, asyncio.Lock] = {}

_THINKING_PLACEHOLDERS = [
    "thinking…",
    "smoking weed…",
    "rolls…",
    "consulting the oracle…",
    "asking your mom…",
    "googling really hard…",
    "buffering…",
    "vibing…",
    "brewing some thoughts…",
    "loading neurons…",
    "warming up the hamsters…",
]


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _summary_locks:
        _summary_locks[chat_id] = asyncio.Lock()
    return _summary_locks[chat_id]


def build_router(config: Config) -> Router:
    router = Router(name="mr-stat")
    allowlist = AllowlistMiddleware(
        config.allowed_chat_ids,
        config.allowed_user_ids,
        config.blocked_user_ids,
        config.sudo_user_ids,
    )
    router.message.middleware(allowlist)
    router.edited_message.middleware(allowlist)
    router.chat_member.middleware(allowlist)

    @router.message(Command("ask"))
    async def cmd_ask(message: Message, bot: Bot) -> None:
        text = message.text or ""
        parts = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""
        if not question:
            await message.reply("Usage: /ask <question>", allow_sending_without_reply=True)
            return
        placeholder = await message.reply(random.choice(_THINKING_PLACEHOLDERS), allow_sending_without_reply=True)
        try:
            answer_text, answer_entities = await ask_question(
                question,
                chat_id=message.chat.id,
                user_id=message.from_user.id if message.from_user else 0,
                db_path=config.db_path,
                api_key=config.llm_api_key,
                base_url=config.llm_base_url,
                model=config.llm_model,
                gemini_api_key=config.gemini_api_key,
                gemini_model_ask=config.gemini_model_ask,
            )
        except Exception:
            tb = traceback.format_exc()
            await log_to_chat(bot, config.logs_chat_id, f"/ask failed:\n{tb}", level=logging.ERROR)
            await placeholder.edit_text("Failed to get an answer.")
            return
        await placeholder.edit_text(answer_text, entities=answer_entities)



    @router.message(Command("summary"))
    async def cmd_summary(message: Message, bot: Bot) -> None:
        user = message.from_user
        lock = _get_lock(message.chat.id)
        if lock.locked():
            await message.reply("Summary is already being generated, wait.", allow_sending_without_reply=True)
            return
        if not (user and user.id in config.sudo_user_ids):
            date_str = datetime.now(ZoneInfo(config.summary_tz)).strftime("%Y-%m-%d")
            calls = await get_summary_calls_today(config.db_path, message.chat.id, date_str)
            if calls >= config.summary_daily_limit:
                last_msg_id = await get_last_summary(config.db_path, message.chat.id)
                link_part = ""
                if last_msg_id:
                    if message.chat.username:
                        link = f"https://t.me/{message.chat.username}/{last_msg_id}"
                    else:
                        numeric = str(message.chat.id).lstrip("-").removeprefix("100")
                        link = f"https://t.me/c/{numeric}/{last_msg_id}"
                    link_part = f"\n{link}"
                await message.reply(
                    f"Limit summaries reached: maximum per day {config.summary_daily_limit}. Last summary: {link_part}",
                    allow_sending_without_reply=True,
                )
                return
            await increment_summary_calls(config.db_path, message.chat.id, date_str)
        placeholder = await message.reply("Generating summary...", allow_sending_without_reply=True)
        async with lock:
            try:
                await _send_summary(
                    bot, config, message.chat.id,
                    placeholder=placeholder, chat_username=message.chat.username,
                )
            except Exception:
                tb = traceback.format_exc()
                await log_to_chat(
                    bot,
                    config.logs_chat_id,
                    f"/summary failed in {chat_ref(message.chat.id, message.chat.username)}:\n{tb}",
                    level=logging.ERROR,
                )
                await placeholder.edit_text("Summary failed. Fuck you. 🤗")

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def on_text(message: Message) -> None:
        text = message.text
        if text is None or text.startswith("/") or message.from_user is None:
            return
        u = message.from_user
        logger.debug(
            "msg chat=%d(%s) user=%d(@%s %s): %s",
            message.chat.id, message.chat.username or message.chat.title,
            u.id, u.username or "", u.full_name,
            text[:120],
        )
        await save_message(
            config.db_path,
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=u.id,
            username=u.username,
            full_name=u.full_name,
            text=text,
            ts=int(message.date.timestamp()),
        )

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.photo)
    async def on_photo(message: Message) -> None:
        if message.from_user is None:
            return
        u = message.from_user
        caption = f" {message.caption}" if message.caption else ""
        logger.debug(
            "photo chat=%d(%s) user=%d(@%s %s)%s",
            message.chat.id, message.chat.username or message.chat.title,
            u.id, u.username or "", u.full_name,
            f" caption={message.caption[:60]}" if message.caption else "",
        )
        await save_message(
            config.db_path,
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=u.id,
            username=u.username,
            full_name=u.full_name,
            text=f"[photo]{caption}",
            ts=int(message.date.timestamp()),
        )

    @router.edited_message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def on_text_edited(message: Message) -> None:
        text = message.text
        if text is None or text.startswith("/") or message.from_user is None:
            return
        u = message.from_user
        logger.debug(
            "edit chat=%d(%s) user=%d(@%s %s): %s",
            message.chat.id, message.chat.username or message.chat.title,
            u.id, u.username or "", u.full_name,
            text[:120],
        )
        await update_message(config.db_path, chat_id=message.chat.id, message_id=message.message_id, text=text)

    @router.edited_message(F.chat.type.in_({"group", "supergroup"}), F.photo)
    async def on_photo_edited(message: Message) -> None:
        if message.from_user is None:
            return
        u = message.from_user
        caption = f" {message.caption}" if message.caption else ""
        logger.debug(
            "edit photo chat=%d(%s) user=%d(@%s %s)%s",
            message.chat.id, message.chat.username or message.chat.title,
            u.id, u.username or "", u.full_name,
            f" caption={message.caption[:60]}" if message.caption else "",
        )
        await update_message(
            config.db_path,
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=f"[photo]{caption}",
        )

    @router.chat_member(F.new_chat_member.status.in_({"kicked", "banned"}))
    async def on_user_banned(event: ChatMemberUpdated, bot: Bot) -> None:
        user = event.new_chat_member.user
        deleted = await delete_user_messages(config.db_path, event.chat.id, user.id)
        logger.info(
            "banned user=%d(@%s %s) in chat=%d(%s): removed %d messages from DB",
            user.id, user.username or "", user.full_name,
            event.chat.id, event.chat.username or event.chat.title,
            deleted,
        )

    return router


async def _send_summary(
    bot: Bot,
    config: Config,
    chat_id: int,
    *,
    placeholder: Message | None = None,
    chat_username: str | None = None,
) -> bool:
    """Build and deliver a summary for `chat_id`. Returns True if a summary was sent."""
    since_ts = int(time.time()) - config.period_seconds
    messages = await get_messages_for_period(config.db_path, chat_id, since_ts)
    per_user = aggregate_by_user(messages, config.min_words)
    if not per_user:
        if placeholder is not None:
            await placeholder.edit_text(
                "Insufficient data for the last 24 hours "
                f"(need at least {config.min_words} words from the user).",
            )
        return False

    text = await build_summary(
        messages,
        per_user,
        api_key=config.llm_api_key,
        base_url=config.llm_base_url,
        model=config.llm_model,
        timezone=config.summary_tz,
        gemini_api_key=config.gemini_api_key,
        gemini_model=config.gemini_model,
    )
    if placeholder is not None:
        sent = await placeholder.edit_text(text, parse_mode=ParseMode.HTML)
    else:
        sent = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    await save_last_summary(config.db_path, chat_id, sent.message_id, int(time.time()))
    await log_to_chat(bot, config.logs_chat_id, f"summary in {chat_ref(chat_id, chat_username)}:\n{text}")
    return True
