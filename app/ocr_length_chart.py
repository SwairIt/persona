"""Per-day OCR character-count timeseries.

v0.65 feature 2/3. Quantifies *how much text* the OCR worker pulled out
of the day's screenshots by summing ``LENGTH(ocr_text)`` over every
shot bucketed into UTC calendar days.

The output is a dense list — every day in the window is present, with
``total_chars`` / ``shot_count`` / ``avg_chars_per_shot`` zero on days
where no OCR landed.  The chart layer can iterate without missing-key
checks.

Three numbers per day:

* ``total_chars``         — sum of ``LENGTH(ocr_text)`` for every
  completed-OCR shot on that day. ``NULL`` rows contribute zero.
* ``shot_count``          — number of completed-OCR shots that day.
  Pending / skipped / failed rows are *not* counted because their
  character contribution would smear the per-shot average as work
  catches up.
* ``avg_chars_per_shot``  — ``total_chars / shot_count`` rounded to one
  decimal place. ``0.0`` when ``shot_count`` is zero so the chart can
  use it as a numeric height without a guard.

The aggregation is bucketed to UTC days to match the rest of the stats
panels (heatmap, hour histogram, error rate). SQLite stores
``datetime('now')`` in UTC already, so the ``DATE(captured_at)``
projection is wire-compatible without an explicit conversion.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr.length")

# Hard bounds on the look-back window — mirrors the route-side
# ``Query(ge=..., le=...)`` so the data layer is safe to call from
# anywhere (CLI, tests, future scheduled jobs) without re-validating.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 60


class OcrLengthDay(TypedDict):
    """One day's bucket in the OCR-length timeseries.

    Attributes:
        date:                ISO ``YYYY-MM-DD`` UTC calendar date.
        total_chars:         Sum of ``LENGTH(ocr_text)`` for every
                             completed-OCR shot captured on this day.
        avg_chars_per_shot:  ``total_chars / shot_count`` to one
                             decimal place. ``0.0`` on empty days so
                             the chart layer can multiply without a
                             zero-divide guard.
        shot_count:          Completed-OCR shots captured on this day.
    """

    date: str
    total_chars: int
    avg_chars_per_shot: float
    shot_count: int


def _clamp_days(days: int) -> int:
    """Clamp ``days`` into ``[_MIN_DAYS, _MAX_DAYS]``."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


def _day_key(captured_at: str) -> str | None:
    """Project an ISO timestamp onto its UTC ``YYYY-MM-DD`` calendar day.

    Mirrors :func:`app.ocr.error_rate._day_key` — SQLite's default
    ``datetime('now')`` shape is ``YYYY-MM-DD HH:MM:SS`` with no
    ``T`` separator and no offset, which pre-3.11 ``fromisoformat``
    rejects.  The fallback slices the first 10 characters and validates
    them as a date so legacy rows still bucket correctly.

    Returns ``None`` when the value is unparseable; the caller drops
    such rows from the aggregate and logs a warning.
    """
    try:
        parsed = datetime.fromisoformat(captured_at)
    except ValueError:
        if len(captured_at) >= 10:
            head = captured_at[:10]
            try:
                date.fromisoformat(head)
            except ValueError:
                return None
            return head
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).date().isoformat()


async def daily_length(days: int = _DEFAULT_DAYS) -> list[OcrLengthDay]:
    """Aggregate per-day OCR character counts over the last ``days`` days.

    Walks every ``ocr_status = 'done'`` screenshot captured within the
    window, projecting each row to ``(day, LENGTH(ocr_text))`` and
    summing per UTC calendar day. ``ocr_text`` columns that are
    ``NULL`` contribute zero characters but still count toward the
    shot total — they're completed OCR results that simply produced
    no text, and excluding them would inflate the per-shot average.

    Args:
        days: Look-back window in days. Clamped to
              ``[_MIN_DAYS, _MAX_DAYS]``.

    Returns:
        A dense list of :class:`OcrLengthDay`, one entry per UTC day
        in the window, oldest first.
    """
    window = _clamp_days(days)

    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=window - 1)
    # Cutoff is the *start* of the earliest UTC day so a shot captured
    # at 00:00:01 still lands inside the window.
    cutoff_iso = datetime.combine(
        start_day, datetime.min.time(), tzinfo=UTC
    ).isoformat()

    chars: dict[str, int] = {}
    counts: dict[str, int] = {}

    rows_scanned = 0
    async with get_connection() as conn:
        # ``COALESCE(LENGTH(ocr_text), 0)`` turns the NULL case into a
        # zero contribution without dropping the shot from the count.
        cursor = await conn.execute(
            "SELECT captured_at AS captured_at, "
            "       COALESCE(LENGTH(ocr_text), 0) AS char_count "
            "FROM screenshots "
            "WHERE ocr_status = 'done' "
            "  AND captured_at IS NOT NULL "
            "  AND captured_at >= ?",
            (cutoff_iso,),
        )
        async for row in cursor:
            rows_scanned += 1
            captured_at = row["captured_at"]
            if captured_at is None:
                continue
            day = _day_key(str(captured_at))
            if day is None:
                log.warning(
                    "ocr.length.bad_captured_at_skipped",
                    captured_at=str(captured_at),
                )
                continue

            counts[day] = counts.get(day, 0) + 1
            chars[day] = chars.get(day, 0) + int(row["char_count"] or 0)

    buckets: list[OcrLengthDay] = []
    for offset in range(window):
        day = (start_day + timedelta(days=offset)).isoformat()
        shot_count = counts.get(day, 0)
        total_chars = chars.get(day, 0)
        avg = (
            round(total_chars / shot_count, 1) if shot_count > 0 else 0.0
        )
        buckets.append(
            OcrLengthDay(
                date=day,
                total_chars=total_chars,
                avg_chars_per_shot=avg,
                shot_count=shot_count,
            )
        )

    log.info(
        "ocr.length.computed",
        days=window,
        rows_scanned=rows_scanned,
        total_chars=sum(chars.values()),
        total_shots=sum(counts.values()),
        non_empty_days=sum(1 for b in buckets if b["shot_count"] > 0),
    )
    return buckets


__all__ = [
    "OcrLengthDay",
    "daily_length",
]
