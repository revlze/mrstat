import asyncio
import logging
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message

from .analytics import aggregate_by_user, build_summary
from .config import Config
from .db import delete_user_messages, get_last_summary, get_messages_for_period, get_summary_calls_today, increment_summary_calls, save_last_summary, save_message, update_message
from .logging_sink import chat_ref, log_to_chat


logger = logging.getLogger(__name__)

_summary_locks: dict[int, asyncio.Lock] = {}


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _summary_locks:
        _summary_locks[chat_id] = asyncio.Lock()
    return _summary_locks[chat_id]


def build_router(config: Config) -> Router:
    router = Router(name="mr-stat")

    @router.message(Command("summary"))
    async def cmd_summary(message: Message, bot: Bot) -> None:
        user = message.from_user
        if user and (user.id == 6297657246): # voodoo
            await message.reply(f"fuck and kill yourself, {message.from_user.full_name}", allow_sending_without_reply=True)
            return
        lock = _get_lock(message.chat.id)
        if lock.locked():
            await message.reply("Summary is already being generated, wait.", allow_sending_without_reply=True)
            return
        if not (user and user.id == 754338369):
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
        async with lock:
            try:
                await _send_summary(
                    bot, config, message.chat.id,
                    reply_to=message, chat_username=message.chat.username,
                )
            except Exception:
                tb = traceback.format_exc()
                await log_to_chat(
                    bot,
                    config.logs_chat_id,
                    f"/summary failed in {chat_ref(message.chat.id, message.chat.username)}:\n{tb}",
                    level=logging.ERROR,
                )
                await message.reply("Summary failed. Fuck you. 🤗", allow_sending_without_reply=True)

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
            text=f"[фото]{caption}",
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
        await update_message(config.db_path, chat_id=message.chat.id, message_id=message.message_id, text=f"[фото]{caption}")

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
    reply_to: Message | None = None,
    chat_username: str | None = None,
) -> bool:
    """Build and deliver a summary for `chat_id`. Returns True if a summary was sent."""
    since_ts = int(time.time()) - config.period_seconds
    messages = await get_messages_for_period(config.db_path, chat_id, since_ts)
    per_user = aggregate_by_user(messages, config.min_words)
    if not per_user:
        if reply_to is not None:
            await reply_to.reply(
                "Insufficient data for the last 24 hours "
                f"(need at least {config.min_words} words from the user).",
                allow_sending_without_reply=True,
            )
        return False

    text = await build_summary(
        messages,
        per_user,
        api_key=config.openrouter_api_key,
        model=config.openrouter_model,
        gemini_api_key=config.gemini_api_key,
        gemini_model=config.gemini_model,
    )
    if reply_to is not None:
        sent = await reply_to.reply(text, allow_sending_without_reply=True, parse_mode=ParseMode.HTML)
    else:
        sent = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    await save_last_summary(config.db_path, chat_id, sent.message_id, int(time.time()))
    await log_to_chat(bot, config.logs_chat_id, f"summary in {chat_ref(chat_id, chat_username)}:\n{text}")
    return True
