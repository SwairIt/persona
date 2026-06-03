"""Per-app capture pause list — apps the capture loop must never shoot.

Some apps are categorically off-limits: a password manager, a banking
page, a private chat. The user marks them here and the capture loop
short-circuits the moment it sees a matching ``app_name`` on the
foreground window — no screenshot is taken, no row is inserted, no
thumbnail is written.

The data shape is deliberately the same as :mod:`app.storage.ocr_skip`:
one normalised app name per row, no metadata beyond a creation
timestamp. Normalisation is ``str.strip().casefold()`` so the operator
can type ``"Bitwarden"``, ``" bitwarden "`` or ``"BITWARDEN"`` and they
all collapse to the same row. The capture loop records ``app_name``
verbatim from the Win32 API, so :func:`is_skipped` is the canonical
place that re-applies the same normalisation before comparing — every
caller goes through it.

Lookup is hot-path: it runs once per capture iteration (i.e. roughly
once per ``capture_interval_seconds``), so it must be cheap. SQLite
gives us a PK-indexed probe; we don't add an in-process cache because
the read happens far less often than the keystroke logger fires and
the WAL-mode write contention from the settings page is negligible.
"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.app_capture_skip")


def _normalise(app_name: str) -> str:
    """Collapse ``app_name`` to the canonical storage form.

    Strips surrounding whitespace and casefolds — both writes and reads
    go through this helper so a user typing ``"Slack "`` and the Win32
    API reporting ``"slack"`` resolve to the same row.
    """
    return app_name.strip().casefold()


async def list_skipped() -> list[str]:
    """Return every paused app, alphabetically sorted by stored name."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name FROM app_capture_skip ORDER BY app_name"
        )
        rows = await cursor.fetchall()
    return [str(row["app_name"]) for row in rows]


async def is_skipped(app_name: str | None) -> bool:
    """Return ``True`` when ``app_name`` is on the capture-pause list.

    ``None`` and empty / whitespace-only strings always return
    ``False`` — we never want to suppress captures whose ``app_name``
    is simply unknown (the foreground-window probe occasionally hands
    back ``None`` for shell surfaces, and dropping those would create
    long blind spots on the timeline).
    """
    if app_name is None:
        return False
    normalised = _normalise(app_name)
    if not normalised:
        return False
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM app_capture_skip WHERE app_name = ? LIMIT 1",
            (normalised,),
        )
        row = await cursor.fetchone()
    return row is not None


async def add(app_name: str) -> None:
    """Insert ``app_name`` into the pause list. Idempotent.

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
            "INSERT OR IGNORE INTO app_capture_skip (app_name) VALUES (?)",
            (normalised,),
        )
        await conn.commit()
    log.info("app_capture_skip.added", app_name=normalised)


async def remove(app_name: str) -> None:
    """Remove ``app_name`` from the pause list. Idempotent — missing rows are fine."""
    normalised = _normalise(app_name)
    if not normalised:
        return
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM app_capture_skip WHERE app_name = ?",
            (normalised,),
        )
        await conn.commit()
    log.info("app_capture_skip.removed", app_name=normalised)


__all__ = [
    "add",
    "is_skipped",
    "list_skipped",
    "remove",
]
