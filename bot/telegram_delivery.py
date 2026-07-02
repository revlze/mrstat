import re
import logging
from html.parser import HTMLParser

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputRichMessage, Message


TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_RICH_TEXT_LIMIT = 32768
logger = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "blockquote",
        "details",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "pre",
        "summary",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self._append_newline()
            self.parts.append("- ")
        elif tag in self._BLOCK_TAGS:
            self._append_newline()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._append_newline()

    def get_text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()

    def _append_newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")


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


def _is_rich_message_fallback_error(exc: TelegramBadRequest) -> bool:
    message = str(exc)
    patterns = (
        r"message[_ ]too[_ ]long",
        r"can't parse",
        r"cannot parse",
        r"failed to parse",
        r"rich[_ ]message",
        r"unsupported start tag",
    )
    return any(re.search(pattern, message, re.IGNORECASE) for pattern in patterns)


def _rich_message_text(rich_message: InputRichMessage) -> str:
    return rich_message.markdown if rich_message.markdown is not None else rich_message.html or ""


async def _send_message_or_reply(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_to: Message | None = None,
) -> Message:
    if reply_to is not None:
        return await reply_to.reply(
            text,
            allow_sending_without_reply=True,
        )
    return await bot.send_message(chat_id, text)


async def _send_rich_message_or_reply(
    bot: Bot,
    chat_id: int,
    rich_message: InputRichMessage,
    *,
    reply_to: Message | None = None,
) -> Message:
    kwargs = {}
    if reply_to is not None:
        kwargs["reply_parameters"] = reply_to.as_reply_parameters(allow_sending_without_reply=True)
        message_thread_id = getattr(reply_to, "message_thread_id", None)
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        direct_topic = getattr(reply_to, "direct_messages_topic", None)
        direct_topic_id = getattr(direct_topic, "topic_id", None)
        if direct_topic_id is not None:
            kwargs["direct_messages_topic_id"] = direct_topic_id
    return await bot.send_rich_message(chat_id, rich_message, **kwargs)


async def _edit_placeholder_or_send(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    placeholder: Message | None = None,
    reply_to: Message | None = None,
) -> Message:
    if placeholder is None:
        return await _send_message_or_reply(
            bot,
            chat_id,
            text,
            reply_to=reply_to,
        )
    try:
        return await placeholder.edit_text(text)
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
        )


async def _edit_rich_placeholder_or_send(
    bot: Bot,
    chat_id: int,
    rich_message: InputRichMessage,
    *,
    placeholder: Message | None = None,
    reply_to: Message | None = None,
) -> Message:
    if placeholder is None:
        return await _send_rich_message_or_reply(bot, chat_id, rich_message, reply_to=reply_to)
    try:
        result = await placeholder.edit_text(rich_message=rich_message)
        if result is True:
            raise RuntimeError("Telegram returned True for a non-inline rich message edit")
        return result
    except TelegramBadRequest as exc:
        if not _is_edit_target_missing_error(exc):
            raise
        logger.info(
            "placeholder message is unavailable; sending a new rich message chat=%d placeholder=%d",
            chat_id,
            placeholder.message_id,
        )
        return await _send_rich_message_or_reply(bot, chat_id, rich_message, reply_to=reply_to)


async def _send_document_notice(
    bot: Bot,
    chat_id: int,
    *,
    placeholder: Message | None = None,
    reply_to: Message | None = None,
    filename: str,
    document_text: str,
) -> Message:
    notice = "Ответ не влез в лимит Telegram-сообщения. Полный текст приложил файлом."
    notice_message = await _edit_placeholder_or_send(
        bot,
        chat_id,
        notice,
        placeholder=placeholder,
        reply_to=reply_to,
    )

    document = BufferedInputFile(document_text.encode("utf-8"), filename=filename)
    if reply_to is not None:
        return await reply_to.reply_document(
            document,
            caption="Полный ответ",
            allow_sending_without_reply=True,
        )
    await bot.send_document(chat_id, document, caption="Полный ответ")
    return notice_message


async def send_text_or_document(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    placeholder: Message | None = None,
    reply_to: Message | None = None,
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
            )
        except TelegramBadRequest as exc:
            if not _is_message_too_long_error(exc):
                raise

    full_text = document_text if document_text is not None else text
    return await _send_document_notice(
        bot,
        chat_id,
        placeholder=placeholder,
        reply_to=reply_to,
        filename=filename,
        document_text=full_text,
    )


async def send_rich_or_document(
    bot: Bot,
    chat_id: int,
    rich_message: InputRichMessage,
    *,
    placeholder: Message | None = None,
    reply_to: Message | None = None,
    fallback_text: str | None = None,
    filename: str,
    document_text: str | None = None,
) -> Message:
    rich_text = _rich_message_text(rich_message)
    if len(rich_text) <= TELEGRAM_RICH_TEXT_LIMIT:
        try:
            return await _edit_rich_placeholder_or_send(
                bot,
                chat_id,
                rich_message,
                placeholder=placeholder,
                reply_to=reply_to,
            )
        except TelegramBadRequest as exc:
            if not _is_rich_message_fallback_error(exc):
                raise
            logger.warning("rich message delivery failed; falling back to plain text/document", exc_info=True)

    plain_text = fallback_text if fallback_text is not None else rich_text
    full_text = document_text if document_text is not None else rich_text
    return await send_text_or_document(
        bot,
        chat_id,
        plain_text,
        placeholder=placeholder,
        reply_to=reply_to,
        filename=filename,
        document_text=full_text,
    )
