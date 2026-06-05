"""Jump-to-time helper: pick the screenshot closest to a target instant.

The web layer (:mod:`app.web.routes.jump_to`) parses ``/goto?at=ISO`` and
relies on :func:`find_closest_shot` here for the actual search. Splitting
the SQL out of the route keeps the route thin and lets future callers
(CLI, agent API) reuse the same logic.

The query is a single parametrised ``SELECT`` ordered by absolute Julian
day difference — SQLite handles that natively via :func:`julianday`,
which is precise enough for second-level deeplinks while staying inside
a sargable expression. We pre-bound the search with a ``BETWEEN`` window
so the absolute-difference sort never has to scan the whole table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.jump_to")


def _parse_target(target_iso: str) -> datetime:
    """Parse ``target_iso`` into a UTC-aware datetime.

    Accepts both the ``Z`` suffix and explicit offsets like ``+03:00``.
    Python 3.12's :meth:`datetime.fromisoformat` already understands
    ``Z``; we keep the explicit replace for clarity and as a guard
    against future runtime changes. Anything that doesn't round-trip
    through ``fromisoformat`` raises :class:`ValueError` — the route
    catches that and returns a 400.
    """
    if not isinstance(target_iso, str) or not target_iso.strip():
        raise ValueError("target_iso must be a non-empty ISO 8601 string")
    candidate = target_iso.strip()
    # Defensive: 3.11 added native ``Z`` support but downgrading to an
    # older interpreter would silently break this path. Normalise first.
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        # An ISO string without an offset is ambiguous — we treat it as
        # UTC rather than guessing the server's local zone. Callers that
        # want local time must include an offset.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def find_closest_shot(
    target_iso: str,
    window_minutes: int = 60,
) -> dict[str, Any] | None:
    """Return the screenshot whose ``captured_at`` is closest to ``target_iso``.

    Args:
        target_iso: ISO 8601 timestamp. Both ``...Z`` and ``...+HH:MM``
            forms are accepted; a missing offset is interpreted as UTC.
            Invalid strings raise :class:`ValueError`.
        window_minutes: Half-width of the search window in minutes.
            Defaults to 60 — i.e. by default we look 1 hour before and
            1 hour after the target. Must be positive.

    Returns:
        A dict with keys ``shot_id``, ``captured_at`` (ISO string),
        ``gap_seconds`` (signed: positive means the shot is after the
        target), ``app_name`` and ``window_title``. ``None`` when no
        screenshot falls inside the window.
    """
    if window_minutes <= 0:
        raise ValueError("window_minutes must be positive")

    target = _parse_target(target_iso)
    half = timedelta(minutes=window_minutes)
    since = target - half
    until = target + half
    target_iso_norm = iso(target)

    log.info(
        "jump_to.search",
        target=target_iso_norm,
        window_minutes=window_minutes,
    )

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, captured_at, app_name, window_title, thumbnail_path
            FROM screenshots
            WHERE captured_at BETWEEN ? AND ?
            ORDER BY ABS(julianday(captured_at) - julianday(?))
            LIMIT 1
            """,
            (iso(since), iso(until), target_iso_norm),
        )
        row = await cursor.fetchone()

    if row is None:
        log.info(
            "jump_to.miss",
            target=target_iso_norm,
            window_minutes=window_minutes,
        )
        return None

    # ``captured_at`` comes back as a string (SQLite stores it that way);
    # parse once so we can compute the gap, then re-emit the canonical
    # ISO form so callers don't have to know the storage encoding.
    captured_raw = str(row["captured_at"])
    captured_dt = _coerce_db_datetime(captured_raw)
    gap_seconds = int((captured_dt - target).total_seconds())

    result: dict[str, Any] = {
        "shot_id": int(row["id"]),
        "captured_at": iso(captured_dt),
        "gap_seconds": gap_seconds,
        "app_name": row["app_name"],
        "window_title": row["window_title"],
    }
    log.info(
        "jump_to.hit",
        target=target_iso_norm,
        shot_id=result["shot_id"],
        gap_seconds=gap_seconds,
    )
    return result


def _coerce_db_datetime(raw: str) -> datetime:
    """Parse the ``captured_at`` string SQLite hands back.

    SQLite stores datetimes as text. Our writer (:func:`app.storage.time.iso`)
    always emits an offset-aware ISO string, but older rows from before
    the timezone migration may be naive — treat those as UTC.
    """
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
