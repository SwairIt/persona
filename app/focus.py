"""Pomodoro-style focus sessions (v0.36).

Thin async API over the ``focus_session`` table introduced by
:mod:`app.storage.migrations.036_focus_sessions`. Kept deliberately
separate from :mod:`app.storage.focus` (the older v0.10 single-duration
sessions) because the v0.36 timer page tracks a different shape — work
minutes + break minutes + a free-form label — and the two tables are
queried by different routes.

Each helper opens its own connection via
:func:`app.storage.db.get_connection`. ``started_at`` / ``ended_at`` are
stored as UTC ISO 8601 strings so the values round-trip cleanly through
SQLite's ``TEXT`` column and through JSON for the client clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.focus")


class FocusSession(TypedDict):
    """One ``focus_session`` row, normalised for templates and JSON."""

    id: int
    started_at: str
    ended_at: str | None
    work_minutes: int
    break_minutes: int
    label: str | None
    completed: bool


def _row_to_session(row: object) -> FocusSession:
    """Convert an ``aiosqlite.Row`` to the public :class:`FocusSession` shape."""
    # ``aiosqlite.Row`` supports both positional and key access; we use key access
    # consistently across the module so the column layout can change without a
    # silent reorder bug.
    ended_at_raw = row["ended_at"]  # type: ignore[index]
    label_raw = row["label"]  # type: ignore[index]
    return FocusSession(
        id=int(row["id"]),  # type: ignore[index]
        started_at=str(row["started_at"]),  # type: ignore[index]
        ended_at=str(ended_at_raw) if ended_at_raw is not None else None,
        work_minutes=int(row["work_minutes"]),  # type: ignore[index]
        break_minutes=int(row["break_minutes"]),  # type: ignore[index]
        label=str(label_raw) if label_raw is not None else None,
        completed=bool(row["completed"]),  # type: ignore[index]
    )


async def start_session(
    work_minutes: int,
    break_minutes: int,
    label: str | None,
) -> int:
    """Insert a new focus session and return its row id.

    The session is left "open" — ``ended_at`` stays ``NULL`` and
    ``completed`` is ``0``. :func:`end_session` flips both columns when
    the timer hits zero or the user bails out.
    """
    started_at = datetime.now(UTC).isoformat()
    clean_label = label.strip() if label else None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO focus_session (started_at, work_minutes, break_minutes, label) "
            "VALUES (?, ?, ?, ?)",
            (started_at, work_minutes, break_minutes, clean_label or None),
        )
        await conn.commit()
        last_id = cursor.lastrowid
    if last_id is None:
        msg = "focus_session insert returned no id"
        raise RuntimeError(msg)
    session_id = int(last_id)
    log.info(
        "focus.start",
        session_id=session_id,
        work_minutes=work_minutes,
        break_minutes=break_minutes,
        has_label=clean_label is not None,
    )
    return session_id


async def end_session(session_id: int, completed: bool) -> None:
    """Mark a session as finished (or bailed) and stamp ``ended_at``."""
    ended_at = datetime.now(UTC).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE focus_session SET ended_at = ?, completed = ? WHERE id = ?",
            (ended_at, 1 if completed else 0, session_id),
        )
        await conn.commit()
    log.info("focus.end", session_id=session_id, completed=completed)


async def current_session() -> FocusSession | None:
    """Return the most recent still-open session, or ``None`` if none."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, started_at, ended_at, work_minutes, break_minutes, label, completed "
            "FROM focus_session "
            "WHERE ended_at IS NULL "
            "ORDER BY started_at DESC "
            "LIMIT 1"
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_session(row)


async def recent_sessions(days: int = 7) -> list[FocusSession]:
    """Return sessions started in the last ``days`` calendar days, newest first."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, started_at, ended_at, work_minutes, break_minutes, label, completed "
            "FROM focus_session "
            "WHERE started_at >= ? "
            "ORDER BY started_at DESC",
            (cutoff,),
        )
        rows = await cursor.fetchall()
    return [_row_to_session(row) for row in rows]
