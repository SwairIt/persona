"""Focus-session distraction blocker (v0.85).

Apps the capture loop must ignore *while a Pomodoro session is running*.
Outside an active :mod:`app.focus` session the list has no effect — the
whole point is that the user is fine with Slack/Telegram/etc. being
captured normally, they just don't want those windows polluting the
timeline of a deep-work block.

The data shape is deliberately the same as
:mod:`app.app_capture_skip` (the v0.67 unconditional pause list) and
``ocr_skip_app``: one normalised app name per row, no metadata beyond a
creation timestamp. Normalisation is ``str.strip().casefold()`` so the
operator can type ``"Slack"``, ``" slack "`` or ``"SLACK"`` and they all
collapse to the same row. The capture loop records ``app_name`` verbatim
from the Win32 API, so :func:`is_blocked` is the canonical place that
re-applies the same normalisation before comparing — every caller goes
through it.

Lookup is hot-path: it runs once per capture iteration. SQLite gives us
a PK-indexed probe; we don't add an in-process cache because writes from
the settings page are negligible and a cache would race with the
"unblock then immediately resume capturing" UX the operator expects.
"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.focus.blocklist")


def _normalise(app_name: str) -> str:
    """Collapse ``app_name`` to the canonical storage form.

    Strips surrounding whitespace and casefolds — both writes and reads
    go through this helper so a user typing ``"Slack "`` and the Win32
    API reporting ``"slack"`` resolve to the same row.
    """
    return app_name.strip().casefold()


async def list_blocked() -> list[str]:
    """Return every blocked app, alphabetically sorted by stored name."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name FROM focus_blocklist ORDER BY app_name"
        )
        rows = await cursor.fetchall()
    return [str(row["app_name"]) for row in rows]


async def is_blocked(app_name: str | None) -> bool:
    """Return ``True`` when ``app_name`` is on the focus blocklist.

    Always probes the database — callers expect a fresh answer because
    the blocklist can be edited live from the settings page while a
    focus session is in progress. ``None`` and empty / whitespace-only
    strings always return ``False`` (we never want to suppress captures
    whose ``app_name`` is simply unknown).
    """
    if app_name is None:
        return False
    normalised = _normalise(app_name)
    if not normalised:
        return False
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM focus_blocklist WHERE app_name = ? LIMIT 1",
            (normalised,),
        )
        row = await cursor.fetchone()
    return row is not None


async def add(app_name: str) -> None:
    """Insert ``app_name`` into the blocklist. Idempotent.

    Raises :class:`ValueError` when the normalised string is empty —
    the settings form is the only caller, and an empty submission is
    a UI bug we want to surface as a 400 rather than silently swallow.
    """
    normalised = _normalise(app_name)
    if not normalised:
        msg = "app_name is required"
        raise ValueError(msg)
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO focus_blocklist (app_name) VALUES (?)",
            (normalised,),
        )
        await conn.commit()
    log.info("focus.blocklist.added", app_name=normalised)


async def remove(app_name: str) -> None:
    """Remove ``app_name`` from the blocklist. Idempotent — missing rows are fine."""
    normalised = _normalise(app_name)
    if not normalised:
        return
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM focus_blocklist WHERE app_name = ?",
            (normalised,),
        )
        await conn.commit()
    log.info("focus.blocklist.removed", app_name=normalised)


__all__ = [
    "add",
    "is_blocked",
    "list_blocked",
    "remove",
]
