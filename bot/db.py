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
CREATE TABLE IF NOT EXISTS summary_calls (
    chat_id  INTEGER NOT NULL,
    date_str TEXT    NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, date_str)
);
"""


@dataclass(frozen=True)
class StoredMessage:
    chat_id: int
    user_id: int
    username: str | None
    full_name: str | None
    text: str
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


async def get_messages_for_period(
    db_path: str, chat_id: int, since_ts: int
) -> list[StoredMessage]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT chat_id, user_id, username, full_name, text, ts "
            "FROM messages WHERE chat_id = ? AND ts >= ? ORDER BY ts ASC",
            (chat_id, since_ts),
        )
        rows = await cursor.fetchall()
    return [StoredMessage(*row) for row in rows]


async def delete_old_messages(db_path: str, before_ts: int) -> int:
    async with aiosqlite.connect(db_path) as conn:
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


async def delete_user_messages(db_path: str, chat_id: int, user_id: int) -> int:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "DELETE FROM messages WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount


async def get_active_chat_ids(db_path: str, since_ts: int) -> list[int]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT chat_id FROM messages WHERE ts >= ?",
            (since_ts,),
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]
