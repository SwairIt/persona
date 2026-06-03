"""Monthly-rollup stats CSV export.

A coarser-grained cousin of :mod:`app.stats_csv`: one row per
``(year-month, app_name)`` over the last ``months_back`` calendar
months (inclusive of the *current* month).  Window is
``[first-of-month(today - months_back-1 months), today]`` — i.e. the
current month plus the preceding ``months_back - 1`` months, so
``months_back=1`` returns just the current month.

Columns:
    ``month``                 — ``YYYY-MM`` (local-month bucket).
    ``app_name``              — foreground app for the bucket.
                                Captures with no detected foreground
                                app are grouped under the literal
                                ``"(unknown)"`` so row counts still
                                reconcile against the raw screenshot
                                table.
    ``shots``                 — exact screenshot count for the bucket.
    ``total_active_seconds``  — gap-capped seconds attributed to
                                *active* shots in this bucket.  Same
                                attribution rule as :mod:`app.stats_csv`
                                — walk per-app chronological pairs,
                                credit gaps ``<= DEFAULT_MAX_GAP_S`` to
                                the *latter* shot's classification, drop
                                gaps that cross app or month boundaries.
                                Idle seconds are deliberately omitted
                                from the monthly rollup: the v0.38
                                per-day CSV already exposes them and the
                                monthly view is meant to be a "where
                                did the working time go?" summary.
    ``ocr_chars_total``       — sum of ``LENGTH(ocr_text)`` over the
                                bucket's screenshots (NULL → 0).

Like :mod:`app.stats_csv`, this function is intentionally self-contained
— no reuse of :mod:`app.idle_stats` / :mod:`app.time_on_app` because
they aggregate at different granularities (whole-day / per-day-per-app
vs per-month-per-app).  SQL is parametrised and CSV escaping goes
through the stdlib :mod:`csv` writer.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.monthly_stats_csv")

DEFAULT_IDLE_THRESHOLD_S = 60
DEFAULT_MAX_GAP_S = 300
_UNKNOWN_APP = "(unknown)"
_CSV_COLUMNS: tuple[str, ...] = (
    "month",
    "app_name",
    "shots",
    "total_active_seconds",
    "ocr_chars_total",
)

_MIN_MONTHS = 1
_MAX_MONTHS = 120


class _Bucket:
    """Mutable accumulator for one ``(month, app_name)`` cell.

    Stored as a class rather than a TypedDict so the gap walk can mutate
    fields in place without typing gymnastics over ``dict.__getitem__``.
    """

    __slots__ = (
        "active_seconds",
        "ocr_chars",
        "prev_dt",
        "shots",
    )

    def __init__(self) -> None:
        self.shots: int = 0
        self.active_seconds: int = 0
        self.ocr_chars: int = 0
        # Tracks the previous shot's timestamp *within this app in this
        # month* so the gap walk only credits same-app adjacent pairs
        # and never bleeds across month boundaries.
        self.prev_dt: datetime | None = None


def _classify_idle(idle_raw: float | int | None, threshold_s: int) -> bool:
    """Return True if the shot counts as *idle*.

    NULL ``idle_seconds`` is treated as *active* (idle_value = 0),
    matching the capture-loop invariant that shots are only persisted
    when idle is below the capture threshold at sample time.
    """
    idle_value = float(idle_raw) if idle_raw is not None else 0.0
    return idle_value >= threshold_s


def _first_of_month(year: int, month: int) -> datetime:
    """Return midnight on the 1st of ``year-month`` (naive local date)."""
    return datetime(year, month, 1)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Add ``delta`` calendar months to ``(year, month)``, wrapping years."""
    # Convert to a zero-based absolute month index for safe arithmetic
    # then back to (year, 1..12).
    idx = year * 12 + (month - 1) + delta
    return idx // 12, (idx % 12) + 1


async def export_monthly_stats_csv(
    months_back: int = 12,
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S,
    max_gap_s: int = DEFAULT_MAX_GAP_S,
) -> str:
    """Build the per-month-per-app CSV body and return it as a string.

    ``months_back`` is clamped to ``[1, 120]`` (10 years) — same spirit
    as the day clamp in :func:`app.stats_csv.export_stats_csv` but in
    monthly units.  The window starts on the 1st of the earliest month
    in scope and ends at *today* (inclusive); shots outside this range
    are filtered out at SQL time.
    """
    months_back = max(months_back, _MIN_MONTHS)
    months_back = min(months_back, _MAX_MONTHS)

    today = datetime.now().astimezone().date()
    # The window starts on the 1st of (today's month - (months_back-1))
    # so months_back=1 yields just the current month.
    start_year, start_month = _shift_month(
        today.year,
        today.month,
        -(months_back - 1),
    )
    start_day = _first_of_month(start_year, start_month).date()
    # End day is today; the SQL filter uses DATE(captured_at) <= today.
    end_day = today

    async with get_connection() as conn:
        # Some legacy schemas predate the ``idle_seconds`` column added
        # in v0.31.  Detect via PRAGMA so we can synthesise a NULL
        # column at SELECT time instead of crashing; the gap walk then
        # treats every shot as *active* (NULL is the same code path as
        # below-threshold), matching the conservative fallback already
        # used in :mod:`app.idle_stats` and :mod:`app.stats_csv`.
        cursor = await conn.execute("PRAGMA table_info(screenshots)")
        cols = {str(r["name"]) for r in await cursor.fetchall()}
        idle_expr = "idle_seconds" if "idle_seconds" in cols else "NULL"

        # All screenshots in window, ordered so the gap walk is
        # deterministic.  Computing ``strftime('%Y-%m', ...)`` and
        # ``DATE(...)`` once on the SQL side keeps Python free of
        # timezone parsing for the bucket key and the window filter.
        cursor = await conn.execute(
            f"SELECT strftime('%Y-%m', captured_at) AS month, "  # noqa: S608 — idle_expr is a hard-coded literal
            f"       captured_at, "
            f"       app_name, "
            f"       {idle_expr} AS idle_seconds, "
            f"       ocr_text "
            f"FROM screenshots "
            f"WHERE DATE(captured_at) >= ? AND DATE(captured_at) <= ? "
            f"ORDER BY month, app_name, captured_at",
            (start_day.isoformat(), end_day.isoformat()),
        )
        raw_rows = list(await cursor.fetchall())

    buckets: dict[tuple[str, str], _Bucket] = {}

    for row in raw_rows:
        month_key = str(row["month"]) if row["month"] is not None else ""
        if not month_key:
            # Defensive: ``strftime()`` should never return NULL given
            # the NOT-NULL ``captured_at`` column, but skip rather than
            # corrupt the bucket key.
            continue

        app_raw = row["app_name"]
        app_key = str(app_raw) if app_raw not in (None, "") else _UNKNOWN_APP

        bucket_key = (month_key, app_key)
        bucket = buckets.get(bucket_key)
        if bucket is None:
            bucket = _Bucket()
            buckets[bucket_key] = bucket

        bucket.shots += 1

        ocr_text = row["ocr_text"]
        if ocr_text is not None:
            bucket.ocr_chars += len(str(ocr_text))

        when = datetime.fromisoformat(str(row["captured_at"]))
        is_idle = _classify_idle(row["idle_seconds"], idle_threshold_s)

        if bucket.prev_dt is not None:
            diff = (when - bucket.prev_dt).total_seconds()
            if 0 < diff <= max_gap_s and not is_idle:
                # Only active gaps land in the monthly rollup; idle
                # gaps are dropped (per the docstring rationale).  The
                # ``prev_dt`` update below still fires unconditionally
                # so a single idle shot in the middle of an active run
                # doesn't merge the surrounding gaps.
                bucket.active_seconds += int(diff)
        bucket.prev_dt = when

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)

    # Stable ordering: month ASC, app_name ASC.  Predictable
    # diff-friendly output beats activity-sorted output for an
    # analysis dump — same convention as :mod:`app.stats_csv`.
    for month_key, app_key in sorted(buckets.keys()):
        bucket = buckets[(month_key, app_key)]
        writer.writerow(
            [
                month_key,
                app_key,
                bucket.shots,
                bucket.active_seconds,
                bucket.ocr_chars,
            ]
        )

    body = buffer.getvalue()

    log.info(
        "monthly_stats_csv.exported",
        months_back=months_back,
        start_day=start_day.isoformat(),
        end_day=end_day.isoformat(),
        rows=len(buckets),
        bytes=len(body),
        idle_threshold_s=idle_threshold_s,
        max_gap_s=max_gap_s,
    )

    return body


__all__ = ["export_monthly_stats_csv"]
