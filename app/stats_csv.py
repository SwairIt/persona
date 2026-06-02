"""Comprehensive per-day-per-app stats CSV export.

One row per ``(date, app_name)`` combination over the last ``days_back``
calendar days (inclusive of *today*; window is
``[today - days_back + 1, today]`` — same convention as the rest of the
stats modules).

Columns:
    ``date``                  — ``YYYY-MM-DD`` (local date of capture).
    ``app_name``              — foreground app for the bucket.  Captures
                                without a known foreground app are
                                grouped under the literal string
                                ``"(unknown)"`` so the row count still
                                reconciles against the raw screenshot
                                table.
    ``shots``                 — exact screenshot count for the bucket.
    ``total_idle_seconds``    — gap-capped seconds attributed to *idle*
                                shots in this bucket; see below.
    ``total_active_seconds``  — gap-capped seconds attributed to
                                *active* shots in this bucket.
    ``ocr_chars_total``       — sum of ``LENGTH(ocr_text)`` over the
                                bucket's screenshots (NULL → 0).
    ``has_tldr``              — ``1`` if this row's ``date`` has a row
                                in ``day_tldr``, else ``0``.

Active-vs-idle attribution mirrors :mod:`app.idle_stats`: a shot whose
``idle_seconds < DEFAULT_IDLE_THRESHOLD_S`` is *active*; ``>=`` the
threshold is *idle*.  Walk the day's shots in chronological order
*scoped to a single app*; for each adjacent pair ``(prev, curr)`` with
matching ``app_name`` whose wall gap is ``<= DEFAULT_MAX_GAP_S``,
attribute the gap to ``total_idle_seconds`` or ``total_active_seconds``
based on the *latter* shot's classification.  App switches and wider
gaps contribute nothing — they're "capture paused" rather than guesses.

The function is intentionally self-contained: it does NOT reuse the
per-day walkers in :mod:`app.idle_stats` / :mod:`app.time_on_app`
because they aggregate at different granularities (whole-day vs
per-app).  The SQL is parametrised and CSV escaping goes through the
stdlib :mod:`csv` writer so commas / quotes / newlines in app names
(rare but possible on Windows) survive the round-trip.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.stats_csv")

DEFAULT_IDLE_THRESHOLD_S = 60
DEFAULT_MAX_GAP_S = 300
_UNKNOWN_APP = "(unknown)"
_CSV_COLUMNS: tuple[str, ...] = (
    "date",
    "app_name",
    "shots",
    "total_idle_seconds",
    "total_active_seconds",
    "ocr_chars_total",
    "has_tldr",
)


class _Bucket:
    """Mutable accumulator for one ``(date, app_name)`` cell.

    Stored as a class rather than a TypedDict so the gap walk can mutate
    fields in place without typing gymnastics over ``dict.__getitem__``.
    """

    __slots__ = (
        "active_seconds",
        "idle_seconds",
        "ocr_chars",
        "prev_dt",
        "shots",
    )

    def __init__(self) -> None:
        self.shots: int = 0
        self.idle_seconds: int = 0
        self.active_seconds: int = 0
        self.ocr_chars: int = 0
        # Tracks the previous shot's timestamp *within this app on this
        # day* so the gap walk only credits same-app adjacent pairs.
        self.prev_dt: datetime | None = None


def _classify_idle(idle_raw: float | int | None, threshold_s: int) -> bool:
    """Return True if the shot counts as *idle*.

    NULL ``idle_seconds`` is treated as *active* (idle_value = 0), matching
    the capture-loop invariant that shots are only persisted when idle is
    below the *capture* threshold at sample time.
    """
    idle_value = float(idle_raw) if idle_raw is not None else 0.0
    return idle_value >= threshold_s


async def export_stats_csv(
    days_back: int = 90,
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S,
    max_gap_s: int = DEFAULT_MAX_GAP_S,
) -> str:
    """Build the per-day-per-app CSV body and return it as a string.

    ``days_back`` is clamped to ``[1, 3650]`` — matches the clamp used by
    :func:`app.ics_export.export_ics`.  Window is
    ``[today - days_back + 1, today]`` inclusive on both ends.
    """
    days_back = max(days_back, 1)
    days_back = min(days_back, 3650)

    today = datetime.now().astimezone().date()
    start_day = today - timedelta(days=days_back - 1)

    async with get_connection() as conn:
        # Some legacy schemas predate the ``idle_seconds`` column added
        # in v0.31.  Detect via PRAGMA so we can synthesise a NULL
        # column at SELECT time instead of crashing; the gap walk then
        # treats every shot as *active* (NULL is the same code path as
        # below-threshold), matching the conservative fallback already
        # used in :mod:`app.idle_stats`.
        cursor = await conn.execute("PRAGMA table_info(screenshots)")
        cols = {str(r["name"]) for r in await cursor.fetchall()}
        idle_expr = "idle_seconds" if "idle_seconds" in cols else "NULL"

        # All screenshots in window, ordered so the gap walk is
        # deterministic.  Selecting ``DATE(captured_at)`` once on the
        # SQL side keeps Python free of timezone parsing for the bucket
        # key.
        cursor = await conn.execute(
            f"SELECT DATE(captured_at) AS day, "  # noqa: S608 — idle_expr is a hard-coded literal
            f"       captured_at, "
            f"       app_name, "
            f"       {idle_expr} AS idle_seconds, "
            f"       ocr_text "
            f"FROM screenshots "
            f"WHERE DATE(captured_at) >= ? AND DATE(captured_at) <= ? "
            f"ORDER BY day, app_name, captured_at",
            (start_day.isoformat(), today.isoformat()),
        )
        raw_rows = list(await cursor.fetchall())

        # Days that have a TL;DR row — one parametrised query so the
        # ``has_tldr`` lookup is O(1) per output row.
        cursor = await conn.execute(
            "SELECT day FROM day_tldr WHERE day >= ? AND day <= ?",
            (start_day.isoformat(), today.isoformat()),
        )
        tldr_days = {str(r["day"]) for r in await cursor.fetchall()}

    buckets: dict[tuple[str, str], _Bucket] = {}

    for row in raw_rows:
        day_key = str(row["day"]) if row["day"] is not None else ""
        if not day_key:
            # Defensive: ``DATE()`` should never return NULL given the
            # NOT-NULL ``captured_at`` column, but skip rather than
            # corrupt the bucket key.
            continue

        app_raw = row["app_name"]
        app_key = str(app_raw) if app_raw not in (None, "") else _UNKNOWN_APP

        bucket_key = (day_key, app_key)
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
            if 0 < diff <= max_gap_s:
                gap = int(diff)
                if is_idle:
                    bucket.idle_seconds += gap
                else:
                    bucket.active_seconds += gap
        bucket.prev_dt = when

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)

    # Stable ordering: date ASC, app_name ASC.  Predictable diff-friendly
    # output beats activity-sorted output for an analysis dump.
    for day_key, app_key in sorted(buckets.keys()):
        bucket = buckets[(day_key, app_key)]
        writer.writerow(
            [
                day_key,
                app_key,
                bucket.shots,
                bucket.idle_seconds,
                bucket.active_seconds,
                bucket.ocr_chars,
                1 if day_key in tldr_days else 0,
            ]
        )

    body = buffer.getvalue()

    log.info(
        "stats_csv.exported",
        days_back=days_back,
        start_day=start_day.isoformat(),
        end_day=today.isoformat(),
        rows=len(buckets),
        bytes=len(body),
        tldr_days=len(tldr_days),
        idle_threshold_s=idle_threshold_s,
        max_gap_s=max_gap_s,
    )

    return body


__all__ = ["export_stats_csv"]
