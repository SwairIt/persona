"""Long-term archive — roll ancient cold screenshots into a satellite SQLite.

After cold tier (no thumbnail, just metadata), once a screenshot is older
than `archive_after_days`, we copy its row to `data/persona_archive.db` and
remove it from the main DB. The archive DB is read-only from the live app's
perspective; it can be opened separately for forensic lookup.

Pinned screenshots are never archived.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.archive")

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_screenshots (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    app_name TEXT,
    window_title TEXT,
    process_name TEXT,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    phash TEXT NOT NULL,
    ocr_text TEXT,
    monitor_index INTEGER NOT NULL DEFAULT 0,
    dedup_group_id INTEGER,
    archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_archived_captured_at
    ON archived_screenshots(captured_at);
CREATE VIRTUAL TABLE IF NOT EXISTS archived_fts USING fts5(
    ocr_text, window_title, app_name,
    content='archived_screenshots',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS archived_ai AFTER INSERT ON archived_screenshots
BEGIN
    INSERT INTO archived_fts(rowid, ocr_text, window_title, app_name)
    VALUES (new.id, COALESCE(new.ocr_text, ''), COALESCE(new.window_title, ''),
            COALESCE(new.app_name, ''));
END;
"""


def archive_db_path() -> Path:
    return get_settings().data_dir / "persona_archive.db"


async def ensure_archive_schema() -> None:
    path = archive_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(ARCHIVE_SCHEMA)
        await conn.commit()


async def archive_cold_older_than(days: int, *, limit: int = 1000) -> int:
    """Move cold rows older than `days` into the archive DB. Returns count."""
    await ensure_archive_schema()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    moved = 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, captured_at, app_name, window_title, process_name,
                   width, height, phash, ocr_text, monitor_index, dedup_group_id
            FROM screenshots
            WHERE tier = 'cold' AND captured_at < ?
            LIMIT ?
            """,
            (cutoff.isoformat(), limit),
        )
        rows = await cursor.fetchall()

    if not rows:
        return 0

    path = archive_db_path()
    async with aiosqlite.connect(path) as adb:
        for row in rows:
            await adb.execute(
                """
                INSERT OR REPLACE INTO archived_screenshots (
                    id, captured_at, app_name, window_title, process_name,
                    width, height, phash, ocr_text, monitor_index, dedup_group_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["id"]),
                    str(row["captured_at"]),
                    row["app_name"],
                    row["window_title"],
                    row["process_name"],
                    int(row["width"]),
                    int(row["height"]),
                    str(row["phash"]),
                    row["ocr_text"],
                    int(row["monitor_index"]),
                    row["dedup_group_id"],
                ),
            )
        await adb.commit()

    async with get_connection() as conn:
        await conn.executemany(
            "DELETE FROM screenshots WHERE id = ?",
            [(int(row["id"]),) for row in rows],
        )
        await conn.commit()
        moved = len(rows)

    log.info("archive.moved", count=moved, cutoff=cutoff.isoformat())
    return moved


async def run_archive_worker(controller) -> None:  # type: ignore[no-untyped-def]
    """Once-a-day sweep that moves cold>archive_after_days rows out."""
    settings = get_settings()
    archive_after_days = getattr(settings, "archive_after_days", 180)

    while not controller.stop_event.is_set():
        try:
            await archive_cold_older_than(archive_after_days)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("archive_worker.failed", error=str(exc))

        try:
            await asyncio.wait_for(controller.stop_event.wait(), timeout=24 * 3600)
        except asyncio.TimeoutError:
            continue
