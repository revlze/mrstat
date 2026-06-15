import logging
import secrets
import time
from typing import Any

from aiohttp import web

from .config import Config
from .db import save_message


logger = logging.getLogger(__name__)

MAX_BODY_SIZE = 16 * 1024
MAX_TEXT_LENGTH = 4096


async def start_ingest_server(config: Config) -> web.AppRunner | None:
    if not config.ingest_enabled:
        return None

    app = web.Application(client_max_size=MAX_BODY_SIZE)
    app["config"] = config
    app.router.add_post("/ingest/message", ingest_message)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.ingest_host, config.ingest_port)
    await site.start()
    logger.info("ingest server started on %s:%d", config.ingest_host, config.ingest_port)
    return runner


async def stop_ingest_server(runner: web.AppRunner | None) -> None:
    if runner is not None:
        await runner.cleanup()


async def ingest_message(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    if not _authorized(request, config):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    try:
        message = _parse_payload(payload, config)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)

    await save_message(config.db_path, **message)
    logger.debug(
        "ingested message chat=%d user=%d(@%s %s): %s",
        message["chat_id"],
        message["user_id"],
        message["username"] or "",
        message["full_name"] or "",
        message["text"][:120],
    )
    return web.json_response({"ok": True})


def _authorized(request: web.Request, config: Config) -> bool:
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {config.ingest_token}"
    return secrets.compare_digest(header, expected)


def _parse_payload(payload: Any, config: Config) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload_must_be_object")

    chat_id = _required_int(payload, "chat_id")
    if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
        raise ValueError("chat_not_allowed")

    username = str(payload.get("username") or "").lstrip("@").lower()
    if username not in config.summary_bot_usernames:
        raise ValueError("bot_username_not_allowed")

    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("text_required")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError("text_too_long")

    return {
        "chat_id": chat_id,
        "message_id": _required_int(payload, "message_id"),
        "user_id": _required_int(payload, "user_id"),
        "username": username,
        "full_name": str(payload.get("full_name") or ""),
        "text": text,
        "ts": int(payload.get("ts") or time.time()),
    }


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if value is None:
        raise ValueError(f"{key}_required")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key}_must_be_int") from exc
