"""High-level CRUD helpers for screenshots, dedup_groups, capture_events, kv_settings."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiosqlite

from app.storage.models import (
    CaptureEvent,
    CaptureEventType,
    DedupGroup,
    OcrStatus,
    Screenshot,
)
from app.storage.time import iso as _iso
from app.storage.time import parse_iso as _parse_iso


async def insert_screenshot(
    conn: aiosqlite.Connection,
    *,
    captured_at: datetime,
    width: int,
    height: int,
    phash: str,
    monitor_index: int = 0,
    thumbnail_path: str | None = None,
    app_name: str | None = None,
    window_title: str | None = None,
    process_name: str | None = None,
    ocr_status: OcrStatus = "pending",
    dedup_group_id: int | None = None,
) -> int:
    """Insert a new screenshot row, return its id."""
    cursor = await conn.execute(
        """
        INSERT INTO screenshots (
            captured_at, monitor_index, width, height, thumbnail_path,
            phash, app_name, window_title, process_name, ocr_status, dedup_group_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _iso(captured_at),
            monitor_index,
            width,
            height,
            thumbnail_path,
            phash,
            app_name,
            window_title,
            process_name,
            ocr_status,
            dedup_group_id,
        ),
    )
    await conn.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        msg = "INSERT did not return a row id"
        raise RuntimeError(msg)
    return row_id


async def get_screenshot(
    conn: aiosqlite.Connection,
    screenshot_id: int,
) -> Screenshot | None:
    """Fetch a screenshot by id."""
    cursor = await conn.execute(
        "SELECT * FROM screenshots WHERE id = ?",
        (screenshot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_screenshot(row)


async def list_screenshots(
    conn: aiosqlite.Connection,
    *,
    limit: int = 200,
    offset: int = 0,
    since: datetime | None = None,
    until: datetime | None = None,
    app_name: str | None = None,
) -> list[Screenshot]:
    """List screenshots ordered by captured_at DESC with optional filters."""
    where: list[str] = []
    params: list[Any] = []

    if since is not None:
        where.append("captured_at >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("captured_at < ?")
        params.append(_iso(until))
    if app_name is not None:
        where.append("app_name = ?")
        params.append(app_name)

    sql = "SELECT * FROM screenshots"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY captured_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [_row_to_screenshot(row) for row in rows]


async def update_screenshot_ocr(
    conn: aiosqlite.Connection,
    screenshot_id: int,
    *,
    ocr_text: str | None,
    ocr_status: OcrStatus,
) -> None:
    """Update OCR text and status for a screenshot."""
    await conn.execute(
        "UPDATE screenshots SET ocr_text = ?, ocr_status = ? WHERE id = ?",
        (ocr_text, ocr_status, screenshot_id),
    )
    await conn.commit()


async def get_neighbour_ids(
    conn: aiosqlite.Connection,
    *,
    screenshot_id: int,
) -> tuple[int | None, int | None]:
    """Return (prev_id, next_id) by captured_at — older on left, newer on right."""
    cursor = await conn.execute(
        "SELECT captured_at FROM screenshots WHERE id = ?",
        (screenshot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return (None, None)
    ts = str(row["captured_at"])

    cursor = await conn.execute(
        "SELECT id FROM screenshots WHERE captured_at < ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (ts,),
    )
    prev_row = await cursor.fetchone()

    cursor = await conn.execute(
        "SELECT id FROM screenshots WHERE captured_at > ? "
        "ORDER BY captured_at ASC LIMIT 1",
        (ts,),
    )
    next_row = await cursor.fetchone()

    prev_id = int(prev_row["id"]) if prev_row else None
    next_id = int(next_row["id"]) if next_row else None
    return (prev_id, next_id)


async def list_pending_ocr(
    conn: aiosqlite.Connection,
    *,
    limit: int = 10,
) -> list[Screenshot]:
    """Return screenshots with ocr_status = 'pending', oldest first."""
    cursor = await conn.execute(
        "SELECT * FROM screenshots WHERE ocr_status = 'pending'"
        " ORDER BY captured_at ASC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [_row_to_screenshot(row) for row in rows]


async def mark_pending_ocr_as_skipped(conn: aiosqlite.Connection) -> int:
    """Set all 'pending' screenshots to 'skipped'. Returns rows affected."""
    cursor = await conn.execute(
        "UPDATE screenshots SET ocr_status = 'skipped' WHERE ocr_status = 'pending'",
    )
    await conn.commit()
    return cursor.rowcount or 0


async def find_dedup_group_by_phash(
    conn: aiosqlite.Connection,
    phash: str,
) -> DedupGroup | None:
    """Lookup a dedup group by exact pHash match."""
    cursor = await conn.execute(
        "SELECT * FROM dedup_groups WHERE phash = ?",
        (phash,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dedup_group(row)


async def list_recent_dedup_groups(
    conn: aiosqlite.Connection,
    *,
    limit: int = 500,
) -> list[DedupGroup]:
    """Recent dedup groups, newest last_seen first."""
    cursor = await conn.execute(
        "SELECT * FROM dedup_groups ORDER BY last_seen DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [_row_to_dedup_group(row) for row in rows]


async def insert_dedup_group(
    conn: aiosqlite.Connection,
    *,
    phash: str,
    representative_screenshot_id: int | None,
    first_seen: datetime,
) -> int:
    """Create a new dedup group, return its id."""
    iso = _iso(first_seen)
    cursor = await conn.execute(
        """
        INSERT INTO dedup_groups (
            representative_screenshot_id, phash, seen_count, first_seen, last_seen
        ) VALUES (?, ?, 1, ?, ?)
        """,
        (representative_screenshot_id, phash, iso, iso),
    )
    await conn.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        msg = "INSERT did not return a row id"
        raise RuntimeError(msg)
    return row_id


async def bump_dedup_group(
    conn: aiosqlite.Connection,
    group_id: int,
    *,
    last_seen: datetime,
) -> None:
    """Increment seen_count and update last_seen for a dedup group."""
    await conn.execute(
        "UPDATE dedup_groups SET seen_count = seen_count + 1, last_seen = ? WHERE id = ?",
        (_iso(last_seen), group_id),
    )
    await conn.commit()


async def set_dedup_group_representative(
    conn: aiosqlite.Connection,
    group_id: int,
    screenshot_id: int,
) -> None:
    """Assign the representative screenshot for a dedup group."""
    await conn.execute(
        "UPDATE dedup_groups SET representative_screenshot_id = ? WHERE id = ?",
        (screenshot_id, group_id),
    )
    await conn.commit()


async def log_capture_event(
    conn: aiosqlite.Connection,
    event_type: CaptureEventType,
    details: dict[str, Any] | None = None,
) -> None:
    """Append a capture-loop event for debugging and stats."""
    payload = json.dumps(details, ensure_ascii=False) if details else None
    await conn.execute(
        "INSERT INTO capture_events (event_type, details) VALUES (?, ?)",
        (event_type, payload),
    )
    await conn.commit()


async def list_capture_events(
    conn: aiosqlite.Connection,
    *,
    limit: int = 200,
    event_type: CaptureEventType | None = None,
) -> list[CaptureEvent]:
    """Most recent capture events."""
    sql = "SELECT * FROM capture_events"
    params: list[Any] = []
    if event_type is not None:
        sql += " WHERE event_type = ?"
        params.append(event_type)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [_row_to_capture_event(row) for row in rows]


async def get_kv(conn: aiosqlite.Connection, key: str) -> str | None:
    """Read a value from the kv_settings table."""
    cursor = await conn.execute(
        "SELECT value FROM kv_settings WHERE key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["value"])


async def set_kv(conn: aiosqlite.Connection, key: str, value: str) -> None:
    """Upsert a kv_settings entry."""
    await conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        (key, value),
    )
    await conn.commit()


async def delete_kv(conn: aiosqlite.Connection, key: str) -> None:
    """Delete a kv_settings entry. No-op if the key is absent."""
    await conn.execute(
        "DELETE FROM kv_settings WHERE key = ?",
        (key,),
    )
    await conn.commit()


async def list_kv(conn: aiosqlite.Connection) -> dict[str, str]:
    """Return all kv_settings as a dict."""
    cursor = await conn.execute("SELECT key, value FROM kv_settings")
    rows = await cursor.fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _row_to_screenshot(row: aiosqlite.Row) -> Screenshot:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    tier = row["tier"] if "tier" in keys else "hot"
    is_private = bool(row["is_private"]) if "is_private" in keys else False
    # v0.70 — defensive read: legacy DBs upgraded in-place may briefly
    # render before migration 067 runs, so we tolerate the column
    # being absent and treat that case as "unlocked".
    locked = bool(row["locked"]) if "locked" in keys else False
    return Screenshot(
        id=row["id"],
        captured_at=_parse_iso(row["captured_at"]),
        monitor_index=row["monitor_index"],
        width=row["width"],
        height=row["height"],
        thumbnail_path=row["thumbnail_path"],
        phash=row["phash"],
        app_name=row["app_name"],
        window_title=row["window_title"],
        process_name=row["process_name"],
        ocr_status=row["ocr_status"],
        ocr_text=row["ocr_text"],
        dedup_group_id=row["dedup_group_id"],
        created_at=_parse_iso(row["created_at"]),
        tier=tier or "hot",
        is_private=is_private,
        locked=locked,
    )


def _row_to_dedup_group(row: aiosqlite.Row) -> DedupGroup:
    return DedupGroup(
        id=row["id"],
        representative_screenshot_id=row["representative_screenshot_id"],
        phash=row["phash"],
        seen_count=row["seen_count"],
        first_seen=_parse_iso(row["first_seen"]),
        last_seen=_parse_iso(row["last_seen"]),
    )


def _row_to_capture_event(row: aiosqlite.Row) -> CaptureEvent:
    return CaptureEvent(
        id=row["id"],
        ts=_parse_iso(row["ts"]),
        event_type=row["event_type"],
        details=row["details"],
    )
