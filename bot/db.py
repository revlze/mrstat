from dataclasses import dataclass

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT,
    full_name  TEXT,
    text       TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);
CREATE TABLE IF NOT EXISTS message_media (
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    media_type     TEXT    NOT NULL,
    file_id        TEXT    NOT NULL,
    media_group_id TEXT,
    mime_type      TEXT    NOT NULL,
    ts             INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id, media_type)
);
CREATE INDEX IF NOT EXISTS idx_message_media_group
    ON message_media(chat_id, media_group_id, message_id);
CREATE TABLE IF NOT EXISTS summary_calls (
    chat_id  INTEGER NOT NULL,
    date_str TEXT    NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, date_str)
);
CREATE TABLE IF NOT EXISTS last_summary (
    chat_id    INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    ts         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ask_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role    TEXT    NOT NULL,
    content TEXT    NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ask_history_chat_user ON ask_history(chat_id, user_id, id);
"""


@dataclass(frozen=True)
class StoredMessage:
    chat_id: int
    message_id: int
    user_id: int
    username: str | None
    full_name: str | None
    text: str
    ts: int


@dataclass(frozen=True)
class StoredMedia:
    chat_id: int
    message_id: int
    media_type: str
    file_id: str
    media_group_id: str | None
    mime_type: str
    ts: int


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def save_message(
    db_path: str,
    *,
    chat_id: int,
    message_id: int,
    user_id: int,
    username: str | None,
    full_name: str | None,
    text: str,
    ts: int,
) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(chat_id, message_id, user_id, username, full_name, text, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, user_id, username, full_name, text, ts),
        )
        await conn.commit()


async def save_message_media(
    db_path: str,
    *,
    chat_id: int,
    message_id: int,
    media_type: str,
    file_id: str,
    media_group_id: str | None,
    mime_type: str,
    ts: int,
) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO message_media "
            "(chat_id, message_id, media_type, file_id, media_group_id, mime_type, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, message_id, media_type) DO UPDATE SET "
            "file_id = excluded.file_id, "
            "media_group_id = excluded.media_group_id, "
            "mime_type = excluded.mime_type, "
            "ts = excluded.ts",
            (chat_id, message_id, media_type, file_id, media_group_id, mime_type, ts),
        )
        await conn.commit()


async def get_messages_for_period(
    db_path: str, chat_id: int, since_ts: int
) -> list[StoredMessage]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT chat_id, message_id, user_id, username, full_name, text, ts "
            "FROM messages WHERE chat_id = ? AND ts >= ? ORDER BY ts ASC",
            (chat_id, since_ts),
        )
        rows = await cursor.fetchall()
    return [StoredMessage(*row) for row in rows]


async def get_message_context(
    db_path: str,
    chat_id: int,
    message_id: int,
    *,
    before: int,
    after: int,
    max_message_id: int | None = None,
) -> list[StoredMessage]:
    upper_bound = max_message_id if max_message_id is not None else 9_223_372_036_854_775_807
    async with aiosqlite.connect(db_path) as conn:
        before_cursor = await conn.execute(
            "SELECT chat_id, message_id, user_id, username, full_name, text, ts "
            "FROM messages "
            "WHERE chat_id = ? AND message_id <= ? "
            "ORDER BY message_id DESC LIMIT ?",
            (chat_id, message_id, before + 1),
        )
        before_rows = await before_cursor.fetchall()
        after_cursor = await conn.execute(
            "SELECT chat_id, message_id, user_id, username, full_name, text, ts "
            "FROM messages "
            "WHERE chat_id = ? AND message_id > ? AND message_id < ? "
            "ORDER BY message_id ASC LIMIT ?",
            (chat_id, message_id, upper_bound, after),
        )
        after_rows = await after_cursor.fetchall()

    rows = list(reversed(before_rows)) + after_rows
    return [StoredMessage(*row) for row in rows]


async def get_message_media_for_ids(
    db_path: str,
    chat_id: int,
    message_ids: list[int],
) -> list[StoredMedia]:
    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT chat_id, message_id, media_type, file_id, media_group_id, mime_type, ts "
            f"FROM message_media WHERE chat_id = ? AND message_id IN ({placeholders}) "
            "ORDER BY message_id ASC",
            (chat_id, *message_ids),
        )
        rows = await cursor.fetchall()
    return [StoredMedia(*row) for row in rows]


async def get_message_media_between(
    db_path: str,
    chat_id: int,
    start_message_id: int,
    end_message_id: int,
) -> list[StoredMedia]:
    if end_message_id < start_message_id:
        return []
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT chat_id, message_id, media_type, file_id, media_group_id, mime_type, ts "
            "FROM message_media "
            "WHERE chat_id = ? AND message_id BETWEEN ? AND ? "
            "ORDER BY message_id ASC",
            (chat_id, start_message_id, end_message_id),
        )
        rows = await cursor.fetchall()
    return [StoredMedia(*row) for row in rows]


async def get_media_group_media(
    db_path: str,
    chat_id: int,
    media_group_id: str,
) -> list[StoredMedia]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT chat_id, message_id, media_type, file_id, media_group_id, mime_type, ts "
            "FROM message_media "
            "WHERE chat_id = ? AND media_group_id = ? "
            "ORDER BY message_id ASC",
            (chat_id, media_group_id),
        )
        rows = await cursor.fetchall()
    return [StoredMedia(*row) for row in rows]


async def delete_old_messages(db_path: str, before_ts: int) -> int:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "DELETE FROM message_media WHERE ts < ?", (before_ts,)
        )
        cursor = await conn.execute(
            "DELETE FROM messages WHERE ts < ?", (before_ts,)
        )
        await conn.commit()
        return cursor.rowcount


async def get_summary_calls_today(db_path: str, chat_id: int, date_str: str) -> int:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT count FROM summary_calls WHERE chat_id = ? AND date_str = ?",
            (chat_id, date_str),
        )
        row = await cursor.fetchone()
    return row[0] if row else 0


async def increment_summary_calls(db_path: str, chat_id: int, date_str: str) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO summary_calls (chat_id, date_str, count) VALUES (?, ?, 1) "
            "ON CONFLICT(chat_id, date_str) DO UPDATE SET count = count + 1",
            (chat_id, date_str),
        )
        await conn.commit()


async def update_message(db_path: str, *, chat_id: int, message_id: int, text: str) -> bool:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "UPDATE messages SET text = ? WHERE chat_id = ? AND message_id = ?",
            (text, chat_id, message_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def delete_user_messages(db_path: str, chat_id: int, user_id: int) -> int:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "DELETE FROM message_media "
            "WHERE chat_id = ? AND message_id IN ("
            "SELECT message_id FROM messages WHERE chat_id = ? AND user_id = ?"
            ")",
            (chat_id, chat_id, user_id),
        )
        cursor = await conn.execute(
            "DELETE FROM messages WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount


async def save_last_summary(db_path: str, chat_id: int, message_id: int, ts: int) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO last_summary (chat_id, message_id, ts) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET message_id = excluded.message_id, ts = excluded.ts",
            (chat_id, message_id, ts),
        )
        await conn.commit()


async def get_last_summary(db_path: str, chat_id: int) -> int | None:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT message_id FROM last_summary WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
    return row[0] if row else None


async def get_ask_history(db_path: str, chat_id: int, user_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT role, content FROM ask_history "
            "WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, user_id, limit),
        )
        rows = await cursor.fetchall()
    return [{"role": row[0], "content": row[1]} for row in reversed(rows)]


async def append_ask_history(db_path: str, chat_id: int, user_id: int, role: str, content: str, ts: int) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO ask_history (chat_id, user_id, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, role, content, ts),
        )
        await conn.commit()


async def get_active_chat_ids(db_path: str, since_ts: int) -> list[int]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT chat_id FROM messages WHERE ts >= ?",
            (since_ts,),
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]
