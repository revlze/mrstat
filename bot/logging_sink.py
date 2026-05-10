import logging

from aiogram import Bot


logger = logging.getLogger(__name__)
MAX_TELEGRAM_LEN = 4000


def chat_ref(chat_id: int, username: str | None) -> str:
    if username:
        return f"chat {chat_id} (https://t.me/{username})"
    return f"chat {chat_id}"


async def log_to_chat(bot: Bot, chat_id: int, text: str, level: int = logging.INFO) -> None:
    logger.log(level, text)
    snippet = text if len(text) <= MAX_TELEGRAM_LEN else text[:MAX_TELEGRAM_LEN] + "…"
    try:
        await bot.send_message(chat_id, snippet)
    except Exception:
        logger.exception("failed to send log message to chat %s", chat_id)
