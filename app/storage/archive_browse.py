"""Read-only browse / search helpers over the satellite archive DB.

The archive DB (`data/persona_archive.db`) holds rows that have been moved
out of the live `screenshots` table once they exceed `archive_after_days`.
It keeps OCR text, FTS5 index, and metadata — but no thumbnails. These
helpers are the only sanctioned way for the web layer to peek inside it.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from app.search.queries import _sanitise_query
from app.storage.archive import archive_db_path, ensure_archive_schema

_SNIPPET_OPEN = "<mark>"
_SNIPPET_CLOSE = "</mark>"
_SNIPPET_ELLIPSIS = "…"
_SNIPPET_TOKENS = 16
_OCR_PREVIEW_CHARS = 400


async def _open() -> aiosqlite.Connection:
    """Open the archive DB with Row factory; caller owns close."""
    await ensure_archive_schema()
    conn = await aiosqlite.connect(archive_db_path())
    conn.row_factory = aiosqlite.Row
    return conn


def _truncate(text: str | None, limit: int = _OCR_PREVIEW_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + _SNIPPET_ELLIPSIS


async def archive_total(conn: aiosqlite.Connection | None = None) -> int:
    """Count rows in the archived_screenshots table.

    Opens its own connection if `conn` is None — useful for one-shot status
    queries; reuses the caller's connection otherwise.
    """
    if conn is not None:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM archived_screenshots")
        row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0

    own = await _open()
    try:
        cursor = await own.execute("SELECT COUNT(*) AS n FROM archived_screenshots")
        row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0
    finally:
        await own.close()


async def archive_search(query: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """FTS5 search over archived OCR/title/app; returns ranked dicts.

    Empty / unsanitisable query returns []. Sanitiser is reused from the
    live search module so that prefix-matching and operator handling stay
    consistent.
    """
    fts_query = _sanitise_query(query)
    if not fts_query:
        return []

    conn = await _open()
    try:
        sql = (
            "SELECT a.id, a.captured_at, a.app_name, a.window_title, a.ocr_text, "
            "snippet(archived_fts, 0, ?, ?, ?, ?) AS snippet "
            "FROM archived_fts "
            "JOIN archived_screenshots a ON a.id = archived_fts.rowid "
            "WHERE archived_fts MATCH ? "
            "ORDER BY bm25(archived_fts) "
            "LIMIT ?"
        )
        cursor = await conn.execute(
            sql,
            (
                _SNIPPET_OPEN,
                _SNIPPET_CLOSE,
                _SNIPPET_ELLIPSIS,
                _SNIPPET_TOKENS,
                fts_query,
                limit,
            ),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": int(row["id"]),
                "captured_at": row["captured_at"],
                "app_name": row["app_name"],
                "window_title": row["window_title"],
                "ocr_text": _truncate(row["ocr_text"]),
                "snippet": row["snippet"] or "",
            }
            for row in rows
        ]
    finally:
        await conn.close()


async def archive_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Most recently archived rows, ordered by archived_at then captured_at."""
    conn = await _open()
    try:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, window_title, ocr_text "
            "FROM archived_screenshots "
            "ORDER BY archived_at DESC, captured_at DESC "
            "LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": int(row["id"]),
                "captured_at": row["captured_at"],
                "app_name": row["app_name"],
                "window_title": row["window_title"],
                "ocr_text": _truncate(row["ocr_text"]),
                "snippet": "",
            }
            for row in rows
        ]
    finally:
        await conn.close()
