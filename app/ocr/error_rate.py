"""OCR error-rate aggregation — daily share of low-confidence / empty shots.

Persona's OCR worker stores two complementary signals for each completed
screenshot:

* ``screenshots.ocr_text``        — the full recognised text (may be empty
  when Tesseract found nothing intelligible on the frame).
* ``ocr_word(screenshot_id, conf, ...)`` — per-word confidence rows in
  ``[0, 100]`` (Tesseract's ``-1`` "ignored row" values are stripped at
  the worker).

A shot is treated as an *error* for the purposes of this panel when
either:

1. its ``ocr_text`` is ``NULL`` or empty (Tesseract returned nothing), or
2. the **average** of its ``ocr_word.conf`` rows is strictly below
   :data:`_LOW_CONF_THRESHOLD` (default ``50``).

Only screenshots whose ``ocr_status = 'done'`` are counted — pending /
skipped / failed rows don't have a meaningful confidence to report on
yet, and folding them in would smear the error-rate as work catches up.

The aggregation is bucketed into UTC calendar days and returned as a
dense list (one entry per day in the window, ``low_conf_or_empty`` /
``pct`` zero when a day saw no completed OCR). The dense shape lets the
chart layer iterate without missing-key checks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr.error_rate")


# Below this average per-shot confidence the shot is counted as an error
# even when ``ocr_text`` itself is non-empty. Mirrors the red-band cutoff
# used by the OCR overlay UI (red < 50, amber 50-79, green >= 80).
_LOW_CONF_THRESHOLD: Final[int] = 50

# Hard upper bound on the look-back window so a query-string-driven scan
# never trips into a full-history aggregation by accident.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365


class ErrorRateDay(TypedDict):
    """One day's bucket in the error-rate timeseries.

    Attributes:
        date:               ISO ``YYYY-MM-DD`` UTC calendar date.
        total_shots:        Completed-OCR shots captured on this day.
        low_conf_or_empty:  Subset of ``total_shots`` whose ``ocr_text``
                            was empty *or* whose mean ``ocr_word.conf``
                            was below :data:`_LOW_CONF_THRESHOLD`.
        pct:                ``low_conf_or_empty / total_shots * 100.0``,
                            rounded to one decimal place. ``0.0`` when
                            ``total_shots`` is zero so the chart layer
                            can use it as a numeric height without a
                            guard.
    """

    date: str
    total_shots: int
    low_conf_or_empty: int
    pct: float


def _clamp_days(days: int) -> int:
    """Clamp ``days`` into ``[_MIN_DAYS, _MAX_DAYS]``."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


def _day_key(captured_at: str) -> str | None:
    """Project an ISO timestamp onto its UTC ``YYYY-MM-DD`` calendar day.

    Falls back to slicing the first 10 characters when the value is a
    plain ``YYYY-MM-DD HH:MM:SS`` SQLite default (no timezone) — SQLite
    stores ``datetime('now')`` in UTC already, so the unparsed slice is
    correct without an explicit conversion. Returns ``None`` when the
    value is malformed; the caller drops such rows from the aggregate.
    """
    try:
        parsed = datetime.fromisoformat(captured_at)
    except ValueError:
        # SQLite's default ``datetime('now')`` shape is
        # "YYYY-MM-DD HH:MM:SS" with no ``T`` separator and no offset.
        # Pre-3.11 Python's ``fromisoformat`` accepted only the strict
        # ISO 8601 form; the space form is still seen in legacy rows.
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


async def error_rate_by_day(days: int = 30) -> list[ErrorRateDay]:
    """Aggregate the per-day OCR error rate over the last ``days`` days.

    Walks every ``ocr_status = 'done'`` screenshot captured within the
    window, computes each shot's mean ``ocr_word.conf`` via a ``LEFT
    JOIN`` (so shots with zero word rows still appear — their text-only
    status is what classifies them), and bins the results into UTC
    calendar days.

    Args:
        days: Look-back window in days. Clamped to ``[1, 365]``.

    Returns:
        A dense list of :class:`ErrorRateDay`, one per UTC day in
        the window, oldest first. ``low_conf_or_empty`` / ``pct`` are
        zero for days with no completed OCR.
    """
    window = _clamp_days(days)

    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=window - 1)
    # Cutoff is the *start* of the earliest UTC day so a shot captured
    # at 00:00:01 still lands inside the window.
    cutoff_iso = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC).isoformat()

    totals: dict[str, int] = {}
    errors: dict[str, int] = {}

    rows_scanned = 0
    async with get_connection() as conn:
        # ``LEFT JOIN`` keeps shots that produced zero ``ocr_word`` rows
        # — they're the "empty OCR" case that the panel exists to
        # surface. ``GROUP BY s.id`` is what lets us compute the
        # per-shot ``AVG(conf)`` rather than a corpus-wide mean.
        cursor = await conn.execute(
            "SELECT s.captured_at AS captured_at, "
            "       s.ocr_text   AS ocr_text, "
            "       AVG(w.conf)  AS avg_conf, "
            "       COUNT(w.id)  AS word_count "
            "FROM screenshots AS s "
            "LEFT JOIN ocr_word AS w ON w.screenshot_id = s.id "
            "WHERE s.ocr_status = 'done' "
            "  AND s.captured_at >= ? "
            "GROUP BY s.id",
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
                    "ocr.error_rate.bad_captured_at_skipped",
                    captured_at=str(captured_at),
                )
                continue

            totals[day] = totals.get(day, 0) + 1

            text_empty = row["ocr_text"] is None or str(row["ocr_text"]) == ""
            avg_conf_raw = row["avg_conf"]
            word_count = int(row["word_count"] or 0)

            # No words at all → there's no conf to average. We don't
            # flag those purely on word-count; the ``text_empty`` arm
            # already catches the genuinely-empty case. A shot with
            # non-empty text but zero word rows would be a worker bug,
            # not a low-quality OCR — leave it alone.
            low_conf = (
                word_count > 0
                and avg_conf_raw is not None
                and float(avg_conf_raw) < _LOW_CONF_THRESHOLD
            )

            if text_empty or low_conf:
                errors[day] = errors.get(day, 0) + 1

    buckets: list[ErrorRateDay] = []
    for offset in range(window):
        day = (start_day + timedelta(days=offset)).isoformat()
        total = totals.get(day, 0)
        bad = errors.get(day, 0)
        pct = round((bad / total * 100.0), 1) if total > 0 else 0.0
        buckets.append(
            ErrorRateDay(
                date=day,
                total_shots=total,
                low_conf_or_empty=bad,
                pct=pct,
            )
        )

    log.info(
        "ocr.error_rate.computed",
        days=window,
        rows_scanned=rows_scanned,
        total_shots=sum(totals.values()),
        low_conf_or_empty=sum(errors.values()),
        non_empty_days=sum(1 for b in buckets if b["total_shots"] > 0),
    )
    return buckets
