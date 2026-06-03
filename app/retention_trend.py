"""Historical day-by-day retention activity — v0.88 feature 3/3.

The retention worker (:mod:`app.workers.retention`) and the recycle-bin
purge job demote screenshots hot -> warm -> cold and eventually hard
delete them. Every privileged sweep that touches a row leaves a trail in
:mod:`app.audit` so an operator can answer "what got deleted on
2026-05-30?" without scanning structlog files. This module turns that
trail into a 60-day trend: a dense list of ``{date, demoted_warm,
demoted_cold, hard_deleted}`` rows the chart route can render as a
stacked area, and the JSON endpoint can dump for tooling.

Design notes
------------
* **Read-only.** Nothing here ever inserts, updates or deletes a row.
  We aggregate ``audit_log`` and return — no side-effects.
* **Dense window.** Every day in the trailing window is present, even
  zero-activity days, so the SVG renderer never needs ``defaultdict``
  guards and the chart's X axis is uniform.
* **Action bucketing.** Audit rows whose ``action`` starts with
  ``retention.`` are sorted into three buckets by substring:

      ``demote_warm``      -> ``demoted_warm``
      ``demote_cold`` / ``cold_delete``  -> ``demoted_cold``
      ``hard_delete`` / ``recycle.purge`` -> ``hard_deleted``

  Anything else under ``retention.*`` (per-app skips, preview probes,
  swept summaries, …) is ignored — it isn't a row-touching event.
* **Lexicographic date bounds.** ``audit_log.ts`` is ISO-8601 text
  (``YYYY-MM-DD HH:MM:SS``), so a bare ``YYYY-MM-DD`` lower bound is a
  safe string comparison. We bind the cutoff as a ``?`` parameter; no
  user input ever touches this query, but the parametrised shape stays
  consistent with the rest of the audit-log readers.

The output is a plain ``list[RetentionTrendEntry]`` shared by the HTML
page and the JSON endpoint, keeping a single source of truth for the
shape.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Final, TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.retention.trend")

_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 3650  # ~10y — same bound the rest of Persona uses.
_DEFAULT_DAYS: Final[int] = 60
_ISO_DATE_FMT: Final[str] = "%Y-%m-%d"


class RetentionTrendEntry(TypedDict):
    """One day's retention-activity counts.

    Each numeric field is the number of audit rows recorded on that
    calendar day whose action matched the corresponding bucket.
    Zero-activity days are still present so callers never need a
    missing-key guard.
    """

    date: str
    demoted_warm: int
    demoted_cold: int
    hard_deleted: int


def _clamp_days(days: int) -> int:
    """Clamp ``days`` into ``[_MIN_DAYS, _MAX_DAYS]``.

    Mirrors the route-side guard so direct callers (CLI, tests) cannot
    blow past the safe window either.
    """
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


def _dense_window(days: int, today: date | None = None) -> list[str]:
    """Return ``days`` ISO date strings ending at ``today`` (inclusive).

    The list is sorted oldest-first so the SVG renderer can walk it
    left-to-right without re-sorting.
    """
    end = today or date.today()
    start = end - timedelta(days=days - 1)
    return [(start + timedelta(days=offset)).strftime(_ISO_DATE_FMT) for offset in range(days)]


def _classify(action: str) -> str | None:
    """Sort an audit ``action`` slug into one of the three trend buckets.

    Returns ``None`` for ``retention.*`` rows that don't represent an
    actual row-touching event (per-app skips, preview probes, sweep
    summaries) — they aren't counted in the chart.
    """
    lowered = action.lower()
    if "demote_warm" in lowered:
        return "demoted_warm"
    if "demote_cold" in lowered or "cold_delete" in lowered:
        return "demoted_cold"
    if "hard_delete" in lowered or "recycle.purge" in lowered:
        return "hard_deleted"
    return None


async def daily_retention_stats(
    days: int = _DEFAULT_DAYS,
) -> list[RetentionTrendEntry]:
    """Return a dense ``days``-entry trend of retention activity.

    Reads ``audit_log`` rows where ``action LIKE 'retention.%'`` and
    aggregates them per calendar day into the three policy buckets the
    UI cares about (warm demotions, cold demotions, hard deletes).

    * Every day in the trailing window is present, even with all-zero
      counts, so the SVG layer can iterate without missing-key guards.
    * The list is sorted oldest-first.
    * Unknown action slugs under ``retention.*`` are ignored (logged at
      ``debug`` once per slug would be too chatty — we just skip).
    * Any :class:`aiosqlite.Error` is swallowed and a dense all-zero
      window is returned so the chart degrades gracefully on a
      transient SQLite hiccup.
    """
    window = _clamp_days(days)
    dense_dates = _dense_window(window)
    # Seed every day with zero counts so the merge below is just an
    # additive update — no guards, no `setdefault` ladders.
    buckets: dict[str, dict[str, int]] = {
        iso: {"demoted_warm": 0, "demoted_cold": 0, "hard_deleted": 0} for iso in dense_dates
    }
    cutoff = dense_dates[0]  # inclusive lower bound, lexicographically safe.

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT DATE(ts) AS day, action, COUNT(*) AS n
                FROM audit_log
                WHERE action LIKE 'retention.%'
                  AND DATE(ts) >= ?
                GROUP BY day, action
                """,
                (cutoff,),
            )
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        log.warning("retention.trend.query_failed", error=str(exc))
        return [
            RetentionTrendEntry(
                date=iso,
                demoted_warm=0,
                demoted_cold=0,
                hard_deleted=0,
            )
            for iso in dense_dates
        ]

    for row in rows:
        raw_day = row["day"]
        if raw_day is None:
            continue
        day = str(raw_day)
        try:
            datetime.strptime(day, _ISO_DATE_FMT)
        except ValueError:
            log.warning("retention.trend.bad_day_skipped", day=day)
            continue
        if day not in buckets:
            # Row dated outside the window (shouldn't happen given the
            # ``DATE(ts) >= ?`` filter, but defensive against tz drift).
            continue
        bucket_key = _classify(str(row["action"]))
        if bucket_key is None:
            continue
        buckets[day][bucket_key] += int(row["n"])

    dense: list[RetentionTrendEntry] = [
        RetentionTrendEntry(
            date=iso,
            demoted_warm=buckets[iso]["demoted_warm"],
            demoted_cold=buckets[iso]["demoted_cold"],
            hard_deleted=buckets[iso]["hard_deleted"],
        )
        for iso in dense_dates
    ]

    total_warm = sum(e["demoted_warm"] for e in dense)
    total_cold = sum(e["demoted_cold"] for e in dense)
    total_delete = sum(e["hard_deleted"] for e in dense)
    log.info(
        "retention.trend.computed",
        days=window,
        total_demoted_warm=total_warm,
        total_demoted_cold=total_cold,
        total_hard_deleted=total_delete,
        non_zero_days=sum(
            1 for e in dense if e["demoted_warm"] or e["demoted_cold"] or e["hard_deleted"]
        ),
    )

    return dense


__all__ = [
    "RetentionTrendEntry",
    "daily_retention_stats",
]
