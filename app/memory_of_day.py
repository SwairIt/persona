"""Memory-of-the-day picker — one random highlight surfaced each morning.

A tiny anniversary-style picker that fuels :mod:`app.workers.memory_of_day_worker`
and the operator-facing test endpoint in
:mod:`app.web.routes.memory_of_day_settings`. The intent is the same as the
old "On this day" page from photo apps: when the operator opens Persona in the
morning, a single notification should make them stop and look at a moment
from the past — a pinned screenshot, the daily-pin one-liner, or just a
random shot from that anniversary date.

Algorithm
=========

1. Pick a random ``years_back`` offset from ``(1, 2, 3)``. Each offset is
   tried in random order so we don't bias toward "1 year ago" every morning.
2. For each candidate offset, shift today's date back by that many years
   (Feb 29 falls back to Feb 28, matching :mod:`app.this_day_replay`).
3. On the shifted date, in order of preference:

   * ``kind="pinned_shot"`` — a screenshot with ``pinned_at`` populated on
     that calendar day (newest pin wins).
   * ``kind="daily_pin"`` — the :mod:`app.daily_pin` one-liner for that
     date.
   * ``kind="random_shot"`` — any screenshot from that day, sampled by
     ``ORDER BY RANDOM()``.

4. The first non-empty offset wins; we return as soon as we have data.
5. When no offset has any signal at all, return ``None``. The worker
   interprets that as "no morning push today" — not an error.

Return shape
============

``pick_memory`` returns a ``MemoryPick`` dict (or ``None``):

* ``kind`` — one of the three strings above.
* ``shot_id`` — int when the kind is shot-based; absent otherwise.
* ``pin_text`` — str when the kind is ``daily_pin``; absent otherwise.
* ``date_iso`` — the shifted calendar date (``YYYY-MM-DD``).
* ``years_back`` — int offset that produced this pick.
* ``summary`` — short single-line human label suitable for the
  notification body.

SQL safety
==========

Every dynamic value is bound via ``?`` placeholders. The ``years_back``
candidates are hard-coded ints; the shifted date is an ISO string we
constructed ourselves. There is no path from user input into the SQL
layer in this module.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.memory_of_day")

#: Anniversary offsets we try, in declared order — the worker randomises
#: them per tick so morning pushes don't always come from "1 year ago".
_DEFAULT_YEARS_BACK: tuple[int, ...] = (1, 2, 3)

#: Cap on the OCR-derived summary so the notification body stays short.
_SUMMARY_OCR_MAX_CHARS = 120

#: Cap on the daily-pin text we copy into the summary. The full pin is
#: at most 500 chars (see :mod:`app.daily_pin`) but the bell row should
#: feel like a single glance.
_SUMMARY_PIN_MAX_CHARS = 200


class MemoryPick(TypedDict, total=False):
    """One memory chosen for the morning push.

    ``total=False`` because ``shot_id`` and ``pin_text`` are mutually
    exclusive — only the field matching ``kind`` is populated.
    """

    kind: str
    shot_id: int
    pin_text: str
    date_iso: str
    years_back: int
    summary: str


def _shift_years_safe(today: date, years_back: int) -> date:
    """Shift ``today`` back by ``years_back`` years; Feb 29 → Feb 28 fallback.

    Mirrors the helper in :mod:`app.this_day_replay` — duplicated here so
    this module stays standalone and can be unit-tested without dragging
    in the replay package.
    """
    target_year = today.year - years_back
    try:
        return today.replace(year=target_year)
    except ValueError:
        if today.month == 2 and today.day == 29:
            return date(target_year, 2, 28)
        raise


def _day_bounds(target: date) -> tuple[str, str]:
    """Return ``[start, end]`` ISO-8601 UTC strings covering ``target``."""
    start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _short(text: str | None, max_chars: int) -> str:
    """Trim ``text`` to ``max_chars`` and collapse internal whitespace.

    Used for both the OCR preview and the daily-pin one-liner — both can
    contain stray newlines we don't want in a notification row.
    """
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "…"


async def _try_pinned_shot(
    conn: aiosqlite.Connection,
    *,
    target: date,
    years: int,
) -> MemoryPick | None:
    """Look for a screenshot pinned on ``target``. Newest pin on that day wins."""
    start_iso, end_iso = _day_bounds(target)
    cursor = await conn.execute(
        "SELECT id, app_name, ocr_text "
        "FROM screenshots "
        "WHERE pinned_at IS NOT NULL "
        "  AND captured_at >= ? AND captured_at <= ? "
        "ORDER BY pinned_at DESC "
        "LIMIT 1",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    shot_id = int(row["id"])
    app_name = row["app_name"]
    ocr_preview = _short(row["ocr_text"], _SUMMARY_OCR_MAX_CHARS)
    parts: list[str] = []
    if app_name:
        parts.append(str(app_name))
    if ocr_preview:
        parts.append(ocr_preview)
    summary = " · ".join(parts) or "закреплённый момент"

    return MemoryPick(
        kind="pinned_shot",
        shot_id=shot_id,
        date_iso=target.isoformat(),
        years_back=years,
        summary=summary,
    )


async def _try_daily_pin(
    conn: aiosqlite.Connection,
    *,
    target: date,
    years: int,
) -> MemoryPick | None:
    """Look for the daily-pin one-liner for ``target``."""
    cursor = await conn.execute(
        "SELECT pin FROM daily_pin WHERE day = ?",
        (target.isoformat(),),
    )
    row = await cursor.fetchone()
    if row is None or row["pin"] is None:
        return None
    pin_text = str(row["pin"]).strip()
    if not pin_text:
        return None

    return MemoryPick(
        kind="daily_pin",
        pin_text=pin_text,
        date_iso=target.isoformat(),
        years_back=years,
        summary=_short(pin_text, _SUMMARY_PIN_MAX_CHARS),
    )


async def _try_random_shot(
    conn: aiosqlite.Connection,
    *,
    target: date,
    years: int,
) -> MemoryPick | None:
    """Sample a random screenshot from ``target`` as a last-resort pick."""
    start_iso, end_iso = _day_bounds(target)
    cursor = await conn.execute(
        "SELECT id, app_name, ocr_text "
        "FROM screenshots "
        "WHERE captured_at >= ? AND captured_at <= ? "
        "ORDER BY RANDOM() "
        "LIMIT 1",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    shot_id = int(row["id"])
    app_name = row["app_name"]
    ocr_preview = _short(row["ocr_text"], _SUMMARY_OCR_MAX_CHARS)
    parts: list[str] = []
    if app_name:
        parts.append(str(app_name))
    if ocr_preview:
        parts.append(ocr_preview)
    summary = " · ".join(parts) or "случайный момент"

    return MemoryPick(
        kind="random_shot",
        shot_id=shot_id,
        date_iso=target.isoformat(),
        years_back=years,
        summary=summary,
    )


async def _pick_for_offset(
    conn: aiosqlite.Connection,
    *,
    today: date,
    years: int,
) -> MemoryPick | None:
    """Try pinned → daily-pin → random for one ``years_back`` offset."""
    try:
        target = _shift_years_safe(today, years)
    except ValueError:
        log.debug(
            "memory_of_day.shift_failed",
            today=today.isoformat(),
            years_back=years,
        )
        return None

    pick = await _try_pinned_shot(conn, target=target, years=years)
    if pick is not None:
        return pick

    pick = await _try_daily_pin(conn, target=target, years=years)
    if pick is not None:
        return pick

    return await _try_random_shot(conn, target=target, years=years)


async def pick_memory(
    years_back: tuple[int, ...] = _DEFAULT_YEARS_BACK,
    *,
    rng: random.Random | None = None,
) -> MemoryPick | None:
    """Pick one memory for today, or return ``None`` when no data exists.

    Parameters
    ----------
    years_back:
        Anniversary offsets to consider, in unspecified order — they are
        shuffled before we try them so the morning push doesn't always
        come from the same offset. Non-positive values are ignored;
        duplicates collapse silently. Defaults to ``(1, 2, 3)``.
    rng:
        Optional :class:`random.Random` for deterministic tests. ``None``
        uses the module-level shared generator.

    The function is read-only — it never writes to the database, never
    pushes a notification, never touches the kv marker. Callers compose
    those side effects themselves (see ``memory_of_day_worker``).
    """
    today = datetime.now(tz=UTC).date()
    randomiser = rng or random

    candidates = sorted({int(y) for y in years_back if int(y) > 0})
    if not candidates:
        log.info("memory_of_day.no_candidates", today=today.isoformat())
        return None

    # Random shuffle so successive mornings rotate through the offsets
    # instead of always picking the same anniversary.
    shuffled = list(candidates)
    randomiser.shuffle(shuffled)

    async with get_connection() as conn:
        for years in shuffled:
            pick = await _pick_for_offset(conn, today=today, years=years)
            if pick is not None:
                log.info(
                    "memory_of_day.picked",
                    today=today.isoformat(),
                    years_back=pick["years_back"],
                    date_iso=pick["date_iso"],
                    kind=pick["kind"],
                )
                return pick

    log.info(
        "memory_of_day.no_data",
        today=today.isoformat(),
        tried=shuffled,
    )
    return None


__all__ = ["MemoryPick", "pick_memory"]
