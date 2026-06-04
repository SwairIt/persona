"""FTS5 search over OCR text + window titles + app names."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import aiosqlite
from pydantic import BaseModel

from app.storage.time import iso as _iso
from app.storage.time import parse_iso as _parse_iso


class SearchHit(BaseModel):
    screenshot_id: int
    captured_at: datetime
    thumbnail_path: str | None
    app_name: str | None
    window_title: str | None
    snippet: str = ""
    rank: float


_FTS_SAFE = re.compile(r"[^\w\s\"\-\.\:АЯЁа-яё]+", re.UNICODE)


def _sanitise_query(query: str) -> str:
    """Replace risky characters and collapse whitespace.

    FTS5 syntax allows quoted phrases and operators; we accept those but
    strip anything that could break the MATCH parser. Result is wrapped
    so that bare words become prefix-matches (more user-friendly).
    """
    cleaned = _FTS_SAFE.sub(" ", query).strip()
    if not cleaned:
        return ""
    if any(ch in cleaned for ch in '"-:'):
        return cleaned
    tokens = [t for t in cleaned.split() if t]
    return " ".join(f"{t}*" for t in tokens)


async def search(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int = 50,
    offset: int = 0,
    since: datetime | None = None,
    until: datetime | None = None,
    app_name: str | None = None,
) -> list[SearchHit]:
    """Run a hybrid FTS5 + filter search."""
    fts_query = _sanitise_query(query)
    where: list[str] = []
    params: list[Any] = []

    if fts_query:
        base_sql = (
            "SELECT s.id, s.captured_at, s.thumbnail_path, s.app_name, s.window_title, "
            "snippet(screenshots_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet, "
            "bm25(screenshots_fts) AS rank "
            "FROM screenshots_fts "
            "JOIN screenshots s ON s.id = screenshots_fts.rowid "
            "WHERE screenshots_fts MATCH ?"
        )
        params.append(fts_query)
    else:
        base_sql = (
            "SELECT s.id, s.captured_at, s.thumbnail_path, s.app_name, s.window_title, "
            "'' AS snippet, 0.0 AS rank "
            "FROM screenshots s WHERE 1=1"
        )

    if since is not None:
        where.append("s.captured_at >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("s.captured_at < ?")
        params.append(_iso(until))
    if app_name is not None:
        where.append("s.app_name = ?")
        params.append(app_name)

    sql = base_sql
    if where:
        sql += " AND " + " AND ".join(where)
    if fts_query:
        sql += " ORDER BY rank LIMIT ? OFFSET ?"
    else:
        sql += " ORDER BY s.captured_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        SearchHit(
            screenshot_id=row["id"],
            captured_at=_parse_iso(row["captured_at"]),
            thumbnail_path=row["thumbnail_path"],
            app_name=row["app_name"],
            window_title=row["window_title"],
            snippet=row["snippet"] or "",
            rank=float(row["rank"]),
        )
        for row in rows
    ]
