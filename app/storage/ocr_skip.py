"""Per-app OCR skip-list.

Some apps generate noisy, useless OCR text (terminals with ANSI escapes,
video players, fullscreen games, etc.). This table records app names for
which the OCR worker should short-circuit: mark the screenshot as
``done`` with empty text and move on, never invoking Tesseract.

App names are normalised to ``str.strip().casefold()`` before storage and
lookup so user input is forgiving (``"Slack "``, ``"SLACK"`` and
``"slack"`` all collapse to the same row). The capture loop records
``app_name`` verbatim, so callers must normalise before comparing.
"""

from __future__ import annotations

from app.storage.db import get_connection


def _normalise(app_name: str) -> str:
    return app_name.strip().casefold()


async def list_skipped() -> list[str]:
    """Return all skipped app names, alphabetically sorted."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name FROM ocr_skip_app ORDER BY app_name"
        )
        rows = await cursor.fetchall()
    return [str(row["app_name"]) for row in rows]


async def is_skipped(app_name: str | None) -> bool:
    """Return ``True`` when ``app_name`` is on the OCR skip-list.

    ``None`` and empty strings always return ``False`` — we never want to
    skip rows whose ``app_name`` is simply unknown.
    """
    if app_name is None:
        return False
    normalised = _normalise(app_name)
    if not normalised:
        return False
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM ocr_skip_app WHERE app_name = ? LIMIT 1",
            (normalised,),
        )
        row = await cursor.fetchone()
    return row is not None


async def add(app_name: str) -> None:
    """Insert ``app_name`` into the skip-list. Idempotent."""
    normalised = _normalise(app_name)
    if not normalised:
        msg = "app_name is required"
        raise ValueError(msg)
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO ocr_skip_app (app_name) VALUES (?)",
            (normalised,),
        )
        await conn.commit()


async def remove(app_name: str) -> None:
    """Remove ``app_name`` from the skip-list. Idempotent."""
    normalised = _normalise(app_name)
    if not normalised:
        return
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM ocr_skip_app WHERE app_name = ?",
            (normalised,),
        )
        await conn.commit()
