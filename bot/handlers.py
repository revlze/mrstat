import asyncio
import logging
import random
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message, ReactionTypeEmoji, TelegramObject

from .analytics import aggregate_by_user, ask_question, build_summary
from .config import Config
from .db import (
    StoredMessage,
    delete_user_messages,
    get_last_summary,
    get_media_group_media,
    get_message_context,
    get_message_media_between,
    get_message_media_for_ids,
    get_messages_for_period,
    get_summary_calls_today,
    increment_summary_calls,
    save_last_summary,
    save_message,
    save_message_media,
    update_message,
)
from .gemini import InlineImage
from .logging_sink import chat_ref, log_to_chat
from .telegram_delivery import html_to_text, send_text_or_document


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

ASK_REPLY_CONTEXT_BEFORE = 8
ASK_REPLY_CONTEXT_AFTER = 8
ASK_REPLY_MEDIA_ID_RADIUS = 30
MAX_ASK_IMAGES = 10


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _summary_locks:
        _summary_locks[chat_id] = asyncio.Lock()
    return _summary_locks[chat_id]


def _username_key(username: str | None) -> str | None:
    return username.lstrip("@").lower() if username else None


def _should_store_author(user: Any, summary_bot_usernames: frozenset[str]) -> bool:
    if user is None:
        return False
    if not user.is_bot:
        return True
    return _is_summary_bot_author(user, summary_bot_usernames)


def _is_summary_bot_author(user: Any, summary_bot_usernames: frozenset[str]) -> bool:
    if user is None or not user.is_bot:
        return False
    username = _username_key(user.username)
    return username in summary_bot_usernames


def _summary_text_from_message(message: Message) -> str | None:
    if message.text is not None:
        if message.text.startswith("/"):
            return None
        return message.text
    if message.photo:
        caption = f" {message.caption}" if message.caption else ""
        return f"[photo]{caption}"
    if message.caption and message.caption.startswith("/"):
        return None
    caption = f" {message.caption}" if message.caption else ""
    if message.animation:
        return f"[animation]{caption}"
    if message.video:
        return f"[video]{caption}"
    if message.document:
        return f"[document]{caption}"
    if message.sticker:
        emoji = f" {message.sticker.emoji}" if message.sticker.emoji else ""
        return f"[sticker]{emoji}"
    return None


async def _save_summary_message(config: Config, message: Message, text: str) -> None:
    await _save_replied_summary_bot_message(config, message)
    u = message.from_user
    if u is None:
        return
    logger.debug(
        "msg chat=%d(%s) user=%d(@%s %s) type=%s: %s",
        message.chat.id, message.chat.username or message.chat.title,
        u.id, u.username or "", u.full_name,
        message.content_type,
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


async def _update_summary_message(config: Config, message: Message, text: str) -> None:
    await _save_replied_summary_bot_message(config, message)
    u = message.from_user
    if u is None:
        return
    logger.debug(
        "edit chat=%d(%s) user=%d(@%s %s) type=%s: %s",
        message.chat.id, message.chat.username or message.chat.title,
        u.id, u.username or "", u.full_name,
        message.content_type,
        text[:120],
    )
    await update_message(config.db_path, chat_id=message.chat.id, message_id=message.message_id, text=text)


async def _save_replied_summary_bot_message(config: Config, message: Message) -> None:
    reply = message.reply_to_message
    if reply is None or not _is_summary_bot_author(reply.from_user, config.summary_bot_usernames):
        return
    text = _summary_text_from_message(reply)
    if text is None:
        logger.debug(
            "ignored replied summary bot message chat=%d(%s) user=%d(@%s %s) type=%s",
            reply.chat.id, reply.chat.username or reply.chat.title,
            reply.from_user.id, reply.from_user.username or "", reply.from_user.full_name,
            reply.content_type,
        )
        return
    u = reply.from_user
    logger.debug(
        "reply context chat=%d(%s) user=%d(@%s %s) type=%s: %s",
        reply.chat.id, reply.chat.username or reply.chat.title,
        u.id, u.username or "", u.full_name,
        reply.content_type,
        text[:120],
    )
    await _save_photo_media(config, reply)
    await save_message(
        config.db_path,
        chat_id=reply.chat.id,
        message_id=reply.message_id,
        user_id=u.id,
        username=u.username,
        full_name=u.full_name,
        text=text,
        ts=int(reply.date.timestamp()),
    )


async def _save_photo_media(config: Config, message: Message) -> None:
    if not message.photo:
        return
    photo = message.photo[-1]
    await save_message_media(
        config.db_path,
        chat_id=message.chat.id,
        message_id=message.message_id,
        media_type="photo",
        file_id=photo.file_id,
        media_group_id=message.media_group_id,
        mime_type="image/jpeg",
        ts=int(message.date.timestamp()),
    )


def _stored_message_from_live(message: Message) -> StoredMessage | None:
    text = _summary_text_from_message(message)
    user = message.from_user
    if text is None or user is None:
        return None
    return StoredMessage(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        text=text,
        ts=int(message.date.timestamp()),
    )


def _display_stored_author(message: StoredMessage) -> str:
    if message.username:
        return f"@{message.username}"
    return message.full_name or str(message.user_id)


def _format_ask_reply_context(
    messages: list[StoredMessage],
    *,
    reply_message_id: int,
    timezone: str,
) -> str:
    if not messages:
        return ""
    lines = ["Контекст сообщения, на которое ответили командой /ask:"]
    tz = ZoneInfo(timezone)
    for item in messages:
        stamp = datetime.fromtimestamp(item.ts, tz).strftime("%Y-%m-%d %H:%M")
        marker = " (replied)" if item.message_id == reply_message_id else ""
        lines.append(f"- {stamp} {_display_stored_author(item)}{marker}: {item.text}")
    return "\n".join(lines)


async def _get_ask_reply_context(
    config: Config,
    message: Message,
) -> tuple[str, list[StoredMessage]]:
    reply = message.reply_to_message
    if reply is None:
        return "", []

    context_messages = await get_message_context(
        config.db_path,
        message.chat.id,
        reply.message_id,
        before=ASK_REPLY_CONTEXT_BEFORE,
        after=ASK_REPLY_CONTEXT_AFTER,
        max_message_id=message.message_id,
    )
    if all(item.message_id != reply.message_id for item in context_messages):
        live = _stored_message_from_live(reply)
        if live is not None:
            context_messages.append(live)
            context_messages.sort(key=lambda item: item.message_id)

    context_text = _format_ask_reply_context(
        context_messages,
        reply_message_id=reply.message_id,
        timezone=config.summary_tz,
    )
    return context_text, context_messages


def _with_ask_reply_context(question: str, context_text: str) -> str:
    if not context_text:
        return question
    return f"{context_text}\n\nВопрос пользователя:\n{question}"


async def _collect_ask_image_refs(
    db_path: str,
    message: Message,
    context_messages: list[StoredMessage],
) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if message.photo:
        if message.media_group_id:
            group_media = await get_media_group_media(db_path, message.chat.id, message.media_group_id)
            refs.extend((media.file_id, media.mime_type) for media in group_media)
        refs.append((message.photo[-1].file_id, "image/jpeg"))

    reply = message.reply_to_message
    if reply is not None:
        if reply.media_group_id:
            group_media = await get_media_group_media(db_path, reply.chat.id, reply.media_group_id)
            refs.extend((media.file_id, media.mime_type) for media in group_media)
        context_message_ids = [item.message_id for item in context_messages]
        media = await get_message_media_for_ids(db_path, reply.chat.id, context_message_ids)
        refs.extend((item.file_id, item.mime_type) for item in media)
        range_start = max(
            min(context_message_ids, default=reply.message_id),
            reply.message_id - ASK_REPLY_MEDIA_ID_RADIUS,
        )
        range_end = min(
            max(context_message_ids, default=reply.message_id),
            reply.message_id + ASK_REPLY_MEDIA_ID_RADIUS,
            message.message_id - 1,
        )
        range_media = await get_message_media_between(db_path, reply.chat.id, range_start, range_end)
        refs.extend((item.file_id, item.mime_type) for item in range_media)
        if reply.photo:
            refs.append((reply.photo[-1].file_id, "image/jpeg"))

    unique_refs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for file_id, mime_type in refs:
        if file_id in seen:
            continue
        seen.add(file_id)
        unique_refs.append((file_id, mime_type))
        if len(unique_refs) >= MAX_ASK_IMAGES:
            break
    return unique_refs


async def _download_inline_image(bot: Bot, file_id: str, mime_type: str) -> InlineImage:
    file = await bot.get_file(file_id)
    if not file.file_path:
        raise RuntimeError("Telegram did not return a file path for the photo")

    buffer = BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    return InlineImage(data=buffer.getvalue(), mime_type=mime_type)


async def _extract_ask_images(
    message: Message,
    bot: Bot,
    db_path: str,
    context_messages: list[StoredMessage],
) -> list[InlineImage]:
    images: list[InlineImage] = []
    refs = await _collect_ask_image_refs(db_path, message, context_messages)
    logger.info("/ask image_refs=%d", len(refs))
    for file_id, mime_type in refs:
        try:
            images.append(await _download_inline_image(bot, file_id, mime_type))
        except Exception:
            logger.warning("could not download ask image file_id=%s", file_id, exc_info=True)
    return images


async def _mark_user_message_done(bot: Bot, message: Message) -> None:
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="👍")],
        )
    except Exception:
        logger.debug(
            "could not set done reaction chat=%d message=%d",
            message.chat.id,
            message.message_id,
            exc_info=True,
        )


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
        text = message.text or message.caption or ""
        parts = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""
        if not question:
            await message.reply(
                "Usage: /ask <question>\nYou can also attach a photo or reply to a photo.",
                allow_sending_without_reply=True,
            )
            return
        placeholder = await message.reply(random.choice(_THINKING_PLACEHOLDERS), allow_sending_without_reply=True)
        try:
            reply_context, context_messages = await _get_ask_reply_context(config, message)
            ask_text = _with_ask_reply_context(question, reply_context)
            images = await _extract_ask_images(message, bot, config.db_path, context_messages)
            logger.info(
                "/ask request chat=%d user=%d provider=%s model=%s images=%d reply_context_messages=%d",
                message.chat.id,
                message.from_user.id if message.from_user else 0,
                "gemini" if config.gemini_api_key else "openai-compatible",
                config.gemini_model_ask if config.gemini_api_key else config.llm_model,
                len(images),
                len(context_messages),
            )
            answer_text, answer_entities, answer_markdown = await ask_question(
                ask_text,
                chat_id=message.chat.id,
                user_id=message.from_user.id if message.from_user else 0,
                db_path=config.db_path,
                api_key=config.llm_api_key,
                base_url=config.llm_base_url,
                model=config.llm_model,
                timeout=config.llm_timeout,
                gemini_api_key=config.gemini_api_key,
                gemini_model_ask=config.gemini_model_ask,
                images=images or None,
                history_question=question,
            )
        except Exception:
            tb = traceback.format_exc()
            await log_to_chat(bot, config.logs_chat_id, f"/ask failed:\n{tb}", level=logging.ERROR)
            await placeholder.edit_text("Failed to get an answer.")
            return
        await send_text_or_document(
            bot,
            message.chat.id,
            answer_text,
            placeholder=placeholder,
            reply_to=message,
            entities=answer_entities,
            filename="ask-answer.md",
            document_text=answer_markdown,
        )
        await _mark_user_message_done(bot, message)

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
                sent = await _send_summary(
                    bot, config, message.chat.id,
                    placeholder=placeholder, reply_to=message, chat_username=message.chat.username,
                )
                if sent:
                    await _mark_user_message_done(bot, message)
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
        text = _summary_text_from_message(message)
        if (
            text is None
            or not _should_store_author(message.from_user, config.summary_bot_usernames)
        ):
            return
        await _save_summary_message(config, message, text)

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.photo)
    async def on_photo(message: Message) -> None:
        await _save_photo_media(config, message)
        text = _summary_text_from_message(message)
        if (
            text is None
            or not _should_store_author(message.from_user, config.summary_bot_usernames)
        ):
            return
        await _save_summary_message(config, message, text)

    @router.message(F.chat.type.in_({"group", "supergroup"}), F.from_user.is_bot)
    async def on_bot_message(message: Message) -> None:
        text = _summary_text_from_message(message)
        if text is None:
            logger.debug(
                "ignored bot message chat=%d(%s) user=%d(@%s %s) type=%s",
                message.chat.id, message.chat.username or message.chat.title,
                message.from_user.id, message.from_user.username or "", message.from_user.full_name,
                message.content_type,
            )
            return
        if not _should_store_author(message.from_user, config.summary_bot_usernames):
            logger.debug(
                "ignored non-summary bot chat=%d(%s) user=%d(@%s %s) type=%s",
                message.chat.id, message.chat.username or message.chat.title,
                message.from_user.id, message.from_user.username or "", message.from_user.full_name,
                message.content_type,
            )
            return
        await _save_summary_message(config, message, text)

    @router.edited_message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def on_text_edited(message: Message) -> None:
        text = _summary_text_from_message(message)
        if (
            text is None
            or not _should_store_author(message.from_user, config.summary_bot_usernames)
        ):
            return
        await _update_summary_message(config, message, text)

    @router.edited_message(F.chat.type.in_({"group", "supergroup"}), F.photo)
    async def on_photo_edited(message: Message) -> None:
        await _save_photo_media(config, message)
        text = _summary_text_from_message(message)
        if (
            text is None
            or not _should_store_author(message.from_user, config.summary_bot_usernames)
        ):
            return
        await _update_summary_message(config, message, text)

    @router.edited_message(F.chat.type.in_({"group", "supergroup"}), F.from_user.is_bot)
    async def on_bot_message_edited(message: Message) -> None:
        text = _summary_text_from_message(message)
        if text is None:
            logger.debug(
                "ignored edited bot message chat=%d(%s) user=%d(@%s %s) type=%s",
                message.chat.id, message.chat.username or message.chat.title,
                message.from_user.id, message.from_user.username or "", message.from_user.full_name,
                message.content_type,
            )
            return
        if not _should_store_author(message.from_user, config.summary_bot_usernames):
            logger.debug(
                "ignored edited non-summary bot chat=%d(%s) user=%d(@%s %s) type=%s",
                message.chat.id, message.chat.username or message.chat.title,
                message.from_user.id, message.from_user.username or "", message.from_user.full_name,
                message.content_type,
            )
            return
        await _update_summary_message(config, message, text)

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
    reply_to: Message | None = None,
    chat_username: str | None = None,
) -> bool:
    """Build and deliver a summary for `chat_id`. Returns True if a summary was sent."""
    since_ts = int(time.time()) - config.period_seconds
    messages = await get_messages_for_period(config.db_path, chat_id, since_ts)
    per_user = aggregate_by_user(messages, config.min_words, config.summary_bot_usernames)
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
        timeout=config.llm_timeout,
        timezone=config.summary_tz,
        gemini_api_key=config.gemini_api_key,
        gemini_model=config.gemini_model,
    )
    sent = await send_text_or_document(
        bot,
        chat_id,
        text,
        placeholder=placeholder,
        reply_to=reply_to,
        parse_mode=ParseMode.HTML,
        filename="summary.md",
        document_text=html_to_text(text),
    )
    await save_last_summary(config.db_path, chat_id, sent.message_id, int(time.time()))
    await log_to_chat(bot, config.logs_chat_id, f"summary in {chat_ref(chat_id, chat_username)}:\n{text}")
    return True
