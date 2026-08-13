"""SQLite state.

Two tables. `items` is the queue; `post_results` records what each platform did with an
item and is what enforces the rule that a succeeded (item, platform) pair is never posted
again — not on Retry, not after a restart. `post_results` is written from Phase 3 on.

Every call here is a single statement plus commit. aiosqlite serialises work onto one
worker thread, so short atomic operations stay safe when several tasks share a connection.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from app.models import Item, ItemSource, ItemStatus

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    profile             TEXT    NOT NULL,
    source              TEXT    NOT NULL,
    drive_file_id       TEXT    UNIQUE,
    filename            TEXT    NOT NULL,
    mime                TEXT    NOT NULL,
    size_bytes          INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL,
    title               TEXT,
    body                TEXT,
    local_path          TEXT,
    hosted_url          TEXT,
    telegram_message_id INTEGER,
    deferred_until      TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS items_profile_status ON items (profile, status);

CREATE TABLE IF NOT EXISTS post_results (
    item_id   INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    platform  TEXT    NOT NULL,
    status    TEXT    NOT NULL,
    url       TEXT,
    detail    TEXT,
    posted_at TEXT    NOT NULL,
    PRIMARY KEY (item_id, platform)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def connect(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.executescript(SCHEMA)
    await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    await conn.commit()
    log.info("database ready at %s (schema v%d)", path, SCHEMA_VERSION)
    return conn


def _to_item(row: aiosqlite.Row) -> Item:
    return Item(
        id=row["id"],
        profile=row["profile"],
        source=ItemSource(row["source"]),
        drive_file_id=row["drive_file_id"],
        filename=row["filename"],
        mime=row["mime"],
        size_bytes=row["size_bytes"],
        status=ItemStatus(row["status"]),
        title=row["title"],
        body=row["body"],
        local_path=row["local_path"],
        hosted_url=row["hosted_url"],
        telegram_message_id=row["telegram_message_id"],
    )


async def add_item(
    conn: aiosqlite.Connection,
    *,
    profile: str,
    source: ItemSource,
    filename: str,
    mime: str,
    size_bytes: int,
    drive_file_id: str | None = None,
) -> Item | None:
    """Insert a queued item, or return None if this Drive file is already known.

    The UNIQUE constraint on drive_file_id is the dedupe, so a file that reappears in a
    listing — or a poll that overlaps the previous one — cannot enqueue twice.
    """
    now = _now()
    try:
        cursor = await conn.execute(
            """
            INSERT INTO items (profile, source, drive_file_id, filename, mime, size_bytes,
                               status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (profile, source, drive_file_id, filename, mime, size_bytes,
             ItemStatus.QUEUED, now, now),
        )
    except aiosqlite.IntegrityError:
        return None
    await conn.commit()
    return await get_item(conn, cursor.lastrowid)


async def get_item(conn: aiosqlite.Connection, item_id: int) -> Item | None:
    async with conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)) as cursor:
        row = await cursor.fetchone()
    return _to_item(row) if row else None


async def _set(conn: aiosqlite.Connection, item_id: int, **columns: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    await conn.execute(
        f"UPDATE items SET {assignments}, updated_at = ? WHERE id = ?",
        (*columns.values(), _now(), item_id),
    )
    await conn.commit()


async def set_status(conn: aiosqlite.Connection, item_id: int, status: ItemStatus) -> None:
    await _set(conn, item_id, status=status)


async def set_local_path(conn: aiosqlite.Connection, item_id: int, path: Path) -> None:
    await _set(conn, item_id, local_path=str(path))


async def set_offered(conn: aiosqlite.Connection, item_id: int, message_id: int) -> None:
    """Mark an item as sent for approval, recording the card's message id."""
    await _set(
        conn,
        item_id,
        status=ItemStatus.PENDING_APPROVAL,
        telegram_message_id=message_id,
        deferred_until=None,
    )


async def defer(conn: aiosqlite.Connection, item_id: int, minutes: int) -> None:
    """⏭ Later: back to the queue, but not re-offered until the cooldown expires —
    otherwise the next poll would immediately show the same card again."""
    until = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    await _set(conn, item_id, status=ItemStatus.QUEUED, deferred_until=until)


async def claim_offerable(conn: aiosqlite.Connection, profile: str) -> list[Item]:
    """Queued items that are due to be offered: never deferred, or past their cooldown."""
    async with conn.execute(
        """
        SELECT * FROM items
        WHERE profile = ? AND status = ?
          AND (deferred_until IS NULL OR deferred_until <= ?)
        ORDER BY id
        """,
        (profile, ItemStatus.QUEUED, _now()),
    ) as cursor:
        return [_to_item(row) for row in await cursor.fetchall()]


async def items_with_status(
    conn: aiosqlite.Connection, statuses: tuple[ItemStatus, ...]
) -> list[Item]:
    placeholders = ", ".join("?" for _ in statuses)
    async with conn.execute(
        f"SELECT * FROM items WHERE status IN ({placeholders}) ORDER BY id", statuses
    ) as cursor:
        return [_to_item(row) for row in await cursor.fetchall()]


async def known_drive_file_ids(conn: aiosqlite.Connection, profile: str) -> set[str]:
    async with conn.execute(
        "SELECT drive_file_id FROM items WHERE profile = ? AND drive_file_id IS NOT NULL",
        (profile,),
    ) as cursor:
        return {row["drive_file_id"] for row in await cursor.fetchall()}


async def counts_by_status(conn: aiosqlite.Connection, profile: str) -> dict[str, int]:
    async with conn.execute(
        "SELECT status, COUNT(*) AS n FROM items WHERE profile = ? GROUP BY status",
        (profile,),
    ) as cursor:
        return {row["status"]: row["n"] for row in await cursor.fetchall()}
