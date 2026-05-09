import logging
import time
import traceback

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from .analytics import aggregate_by_user, build_summary
from .config import Config
from .db import get_messages_for_period, save_message
from .logging_sink import log_to_chat


logger = logging.getLogger(__name__)
PERIOD_SECONDS = 24 * 3600


def build_router(config: Config) -> Router:
    router = Router(name="mr-stat")

    @router.message(Command("summary"))
    async def cmd_summary(message: Message, bot: Bot) -> None:
        try:
            await _send_summary(bot, config, message.chat.id, reply_to=message)
        except Exception:
            tb = traceback.format_exc()
            await log_to_chat(
                bot,
                config.logs_chat_id,
                f"/summary failed in chat {message.chat.id}:\n{tb}",
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
) -> bool:
    """Build and deliver a summary for `chat_id`. Returns True if a summary was sent."""
    since_ts = int(time.time()) - PERIOD_SECONDS
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
    )
    if reply_to is not None:
        await reply_to.reply(text, allow_sending_without_reply=True)
    else:
        await bot.send_message(chat_id, text)
    await log_to_chat(bot, config.logs_chat_id, f"summary in chat {chat_id}:\n{text}")
    return True
