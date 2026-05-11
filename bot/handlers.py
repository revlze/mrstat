import logging
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from .analytics import aggregate_by_user, build_summary
from .config import Config
from .db import get_messages_for_period, get_summary_calls_today, increment_summary_calls, save_message
from .logging_sink import chat_ref, log_to_chat


logger = logging.getLogger(__name__)


def build_router(config: Config) -> Router:
    router = Router(name="mr-stat")

    @router.message(Command("summary"))
    async def cmd_summary(message: Message, bot: Bot) -> None:
        user = message.from_user
        if user and (user.id == 6297657246 or (user.username or "").lower() == "voodoo"):
            await message.reply("fuck yourself, voodoo", allow_sending_without_reply=True)
            return
        if not (user and user.id == 754338369):
            date_str = datetime.now(ZoneInfo(config.summary_tz)).strftime("%Y-%m-%d")
            calls = await get_summary_calls_today(config.db_path, message.chat.id, date_str)
            if calls >= config.summary_daily_limit:
                await message.reply(
                    f"Limit summaries reached: maximum per day {config.summary_daily_limit} ",
                    allow_sending_without_reply=True,
                )
                return
            await increment_summary_calls(config.db_path, message.chat.id, date_str)
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
                f"/summary failed in {_chat_ref(message.chat.id, message.chat.username)}:\n{tb}",
                level=logging.ERROR,
            )
            await message.reply("Summary failed. Fuck you. 🤗", allow_sending_without_reply=True)

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def on_text(message: Message) -> None:
        text = message.text
        if text is None or text.startswith("/") or message.from_user is None:
            return
        await save_message(
            config.db_path,
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            text=text,
            ts=int(message.date.timestamp()),
        )

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.photo)
    async def on_photo(message: Message) -> None:
        if message.from_user is None:
            return
        caption = f" {message.caption}" if message.caption else ""
        await save_message(
            config.db_path,
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            text=f"[фото]{caption}",
            ts=int(message.date.timestamp()),
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
        per_user,
        api_key=config.openrouter_api_key,
        model=config.openrouter_model,
        gemini_api_key=config.gemini_api_key,
        gemini_model=config.gemini_model,
    )
    if reply_to is not None:
        await reply_to.reply(text, allow_sending_without_reply=True)
    else:
        await bot.send_message(chat_id, text)
    await log_to_chat(bot, config.logs_chat_id, f"summary in {chat_ref(chat_id, chat_username)}:\n{text}")
    return True
