import re
import logging
from html.parser import HTMLParser

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, Message


TELEGRAM_TEXT_LIMIT = 4096
logger = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)


def html_to_text(text: str) -> str:
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    return parser.get_text()


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _is_message_too_long_error(exc: TelegramBadRequest) -> bool:
    return bool(re.search(r"message[_ ]too[_ ]long", str(exc), re.IGNORECASE))


def _is_edit_target_missing_error(exc: TelegramBadRequest) -> bool:
    return bool(re.search(r"message to edit not found", str(exc), re.IGNORECASE))


async def _send_message_or_reply(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_to: Message | None = None,
    entities: list | None = None,
    parse_mode: ParseMode | str | None = None,
) -> Message:
    if reply_to is not None:
        return await reply_to.reply(
            text,
            entities=entities,
            parse_mode=parse_mode,
            allow_sending_without_reply=True,
        )
    return await bot.send_message(chat_id, text, entities=entities, parse_mode=parse_mode)


async def _edit_placeholder_or_send(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    placeholder: Message | None = None,
    reply_to: Message | None = None,
    entities: list | None = None,
    parse_mode: ParseMode | str | None = None,
) -> Message:
    if placeholder is None:
        return await _send_message_or_reply(
            bot,
            chat_id,
            text,
            reply_to=reply_to,
            entities=entities,
            parse_mode=parse_mode,
        )
    try:
        return await placeholder.edit_text(text, entities=entities, parse_mode=parse_mode)
    except TelegramBadRequest as exc:
        if not _is_edit_target_missing_error(exc):
            raise
        logger.info(
            "placeholder message is unavailable; sending a new message chat=%d placeholder=%d",
            chat_id,
            placeholder.message_id,
        )
        return await _send_message_or_reply(
            bot,
            chat_id,
            text,
            reply_to=reply_to,
            entities=entities,
            parse_mode=parse_mode,
        )


async def send_text_or_document(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    placeholder: Message | None = None,
    reply_to: Message | None = None,
    entities: list | None = None,
    parse_mode: ParseMode | str | None = None,
    filename: str,
    document_text: str | None = None,
) -> Message:
    if _utf16_len(text) <= TELEGRAM_TEXT_LIMIT:
        try:
            return await _edit_placeholder_or_send(
                bot,
                chat_id,
                text,
                placeholder=placeholder,
                reply_to=reply_to,
                entities=entities,
                parse_mode=parse_mode,
            )
        except TelegramBadRequest as exc:
            if not _is_message_too_long_error(exc):
                raise

    notice = "Ответ не влез в лимит Telegram-сообщения. Полный текст приложил файлом."
    notice_message = await _edit_placeholder_or_send(
        bot,
        chat_id,
        notice,
        placeholder=placeholder,
        reply_to=reply_to,
    )

    full_text = document_text if document_text is not None else text
    document = BufferedInputFile(full_text.encode("utf-8"), filename=filename)
    if reply_to is not None:
        return await reply_to.reply_document(
            document,
            caption="Полный ответ",
            allow_sending_without_reply=True,
        )
    await bot.send_document(chat_id, document, caption="Полный ответ")
    return notice_message
