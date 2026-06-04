"""Per-app OCR confidence aggregation — which apps OCR badly?

The OCR worker stores a per-word Tesseract confidence score (``0-100``)
in :data:`ocr_word.conf`. Some apps consistently produce low scores
because of how their UI is painted (Cursor's dark theme with thin
anti-aliased glyphs is a classic offender), while others are reliably
readable (Chrome, Office) and don't need a vision-fallback path.

This module rolls those per-word rows up to a per-``app_name`` summary so
the stats panel can show, at a glance, which apps the operator should
either route through the vision OCR fallback or skip outright.

Behaviour:

* Joins :data:`screenshots` with :data:`ocr_word` over the last ``days``
  UTC calendar days and aggregates per ``app_name``: ``words`` (count),
  ``avg`` (mean conf), ``p10`` and ``p90`` (10th / 90th percentile —
  the worst and best slice of the app's words).
* SQLite has no built-in ``PERCENTILE_CONT``, so we fetch the raw
  per-word conf values per app and compute percentiles in Python via
  linear interpolation. The fetch is bounded by the ``days`` cutoff and
  is keyed off the indexed ``ocr_word.screenshot_id`` join.
* Apps with fewer than :data:`_OTHER_BUCKET_THRESHOLD` words are folded
  into a synthetic ``"other"`` bucket so the chart isn't dominated by
  long-tail noise.
* Returns the list ordered by ``avg`` ascending — worst apps first,
  which is what the operator wants to see when tuning settings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr_confidence")

# Hard bounds on the look-back window — mirrors the route-side
# ``Query(ge=..., le=...)`` so the data layer is safe to call from any
# entry point (CLI, tests, future scheduled jobs) without re-validating.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 7

# Apps with fewer than this many word rows inside the window are folded
# into the synthetic ``"other"`` bucket. The threshold is large enough
# that the bucketed app's percentile estimate is reasonably stable
# (100 words ≈ ~5 typical screenshots) but small enough that a regularly
# used app surfaces on its own row.
_OTHER_BUCKET_THRESHOLD: Final[int] = 100

# Sentinel ``app_name`` used for screenshots whose foreground app could
# not be resolved by the capture worker. We keep them visible as a
# dedicated row rather than folding into ``"other"`` so the operator can
# see how often capture is firing without an app name attached.
_UNKNOWN_APP_LABEL: Final[str] = "(unknown)"

# Synthetic bucket label for the long tail of low-word-count apps. Kept
# in sync with the route + template so any rendering of "other" lines up
# across layers.
_OTHER_LABEL: Final[str] = "other"


class AppConfidence(TypedDict):
    """One app's OCR-confidence summary row.

    Attributes:
        app_name:  The capture-worker ``app_name`` value, or
                   :data:`_OTHER_LABEL` for the folded long-tail bucket,
                   or :data:`_UNKNOWN_APP_LABEL` for shots with no
                   resolved app.
        words:     Total ``ocr_word`` rows aggregated for this app.
        avg:       Mean ``conf`` across all of the app's words, rounded
                   to one decimal place.
        p10:       10th-percentile ``conf`` — the worst 10% of the app's
                   words fall at or below this value.
        p90:       90th-percentile ``conf`` — the best 10% of the app's
                   words sit at or above this value.
    """

    app_name: str
    words: int
    avg: float
    p10: float
    p90: float


def _clamp_days(days: int) -> int:
    """Clamp ``days`` into ``[_MIN_DAYS, _MAX_DAYS]``."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


def _percentile(sorted_values: list[int], fraction: float) -> float:
    """Linear-interpolated percentile of a pre-sorted integer list.

    ``fraction`` is in ``[0.0, 1.0]``. The implementation matches the
    "linear" mode of :func:`numpy.percentile` without taking the numpy
    dependency: pick the fractional index ``(n - 1) * fraction`` into
    the sorted slice and interpolate between the bracketing samples.

    For an empty input returns ``0.0`` so callers can use the result as
    a numeric chart height without a guard.
    """
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_values[0])
    if fraction <= 0.0:
        return float(sorted_values[0])
    if fraction >= 1.0:
        return float(sorted_values[-1])
    pos = (n - 1) * fraction
    lower_idx = int(pos)
    upper_idx = lower_idx + 1
    if upper_idx >= n:
        return float(sorted_values[lower_idx])
    weight = pos - lower_idx
    lower = float(sorted_values[lower_idx])
    upper = float(sorted_values[upper_idx])
    return lower + (upper - lower) * weight


def _summarise(app_label: str, confs: list[int]) -> AppConfidence:
    """Roll a list of raw conf values into the per-app summary row.

    Sorts the input in place to amortise the percentile computation —
    callers don't need the original ordering after this returns, and an
    explicit copy would double peak memory for large apps.
    """
    confs.sort()
    total = len(confs)
    if total == 0:
        # Defensive — the caller skips empty groups, but keep the
        # function total: it's also reachable from tests.
        return AppConfidence(
            app_name=app_label,
            words=0,
            avg=0.0,
            p10=0.0,
            p90=0.0,
        )
    mean = sum(confs) / total
    return AppConfidence(
        app_name=app_label,
        words=total,
        avg=round(mean, 1),
        p10=round(_percentile(confs, 0.10), 1),
        p90=round(_percentile(confs, 0.90), 1),
    )


async def compute_app_confidence(
    days: int = _DEFAULT_DAYS,
) -> list[AppConfidence]:
    """Aggregate per-app OCR confidence over the last ``days`` UTC days.

    Joins :data:`screenshots` with :data:`ocr_word` on the indexed
    ``screenshot_id`` foreign key, filters by ``captured_at`` against
    the window cutoff, and groups the resulting conf values per
    ``app_name`` in-process.

    Args:
        days: Look-back window in UTC days. Clamped to
              ``[_MIN_DAYS, _MAX_DAYS]``.

    Returns:
        Per-app summaries ordered by mean confidence ascending — the
        *worst* apps first, which is what the operator wants when
        deciding which apps to vision-fallback or skip.
    """
    window = _clamp_days(days)

    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=window - 1)
    cutoff_iso = datetime.combine(
        start_day, datetime.min.time(), tzinfo=UTC,
    ).isoformat()

    # Collect raw conf samples per app. We don't aggregate in SQL
    # because we need the full sample to compute percentiles in Python
    # — SQLite ships no PERCENTILE_CONT. Memory is bounded by the
    # window cutoff: at ~50 words / shot * a few hundred shots / day
    # this stays well under a megabyte for typical 7-day windows.
    by_app: dict[str, list[int]] = {}
    rows_scanned = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT s.app_name AS app_name, "
            "       w.conf     AS conf "
            "FROM ocr_word AS w "
            "JOIN screenshots AS s ON s.id = w.screenshot_id "
            "WHERE s.captured_at IS NOT NULL "
            "  AND s.captured_at >= ? "
            "  AND w.conf IS NOT NULL ",
            (cutoff_iso,),
        )
        async for row in cursor:
            rows_scanned += 1
            raw_app = row["app_name"]
            label = (
                str(raw_app).strip()
                if raw_app is not None and str(raw_app).strip() != ""
                else _UNKNOWN_APP_LABEL
            )
            try:
                conf_value = int(row["conf"])
            except (TypeError, ValueError):
                continue
            # Defensive — schema enforces conf >= 0, but a manual edit
            # could insert Tesseract's raw -1 "ignored row" marker.
            if conf_value < 0:
                continue
            by_app.setdefault(label, []).append(conf_value)

    # Split apps into "named" rows (>= threshold words) and the long-tail
    # bucket that gets folded into ``"other"``. We fold *after* the
    # per-app collection so the bucketed apps still contribute their
    # raw samples to the synthetic row's percentile calculation rather
    # than losing precision to a pre-aggregated mean.
    summaries: list[AppConfidence] = []
    other_pool: list[int] = []
    folded_apps = 0
    for app_label, confs in by_app.items():
        if len(confs) < _OTHER_BUCKET_THRESHOLD:
            other_pool.extend(confs)
            folded_apps += 1
            continue
        summaries.append(_summarise(app_label, confs))

    if other_pool:
        summaries.append(_summarise(_OTHER_LABEL, other_pool))

    # Worst-first ordering: low mean conf at the top. Tie-break by word
    # count descending so a tied app with more evidence ranks above one
    # with less — the chart should de-prioritise low-sample-size rows.
    summaries.sort(key=lambda r: (r["avg"], -r["words"]))

    log.info(
        "ocr_confidence.computed",
        days=window,
        rows_scanned=rows_scanned,
        named_apps=len(summaries) - (1 if other_pool else 0),
        folded_apps=folded_apps,
        total_words=sum(r["words"] for r in summaries),
    )
    return summaries


__all__ = [
    "AppConfidence",
    "compute_app_confidence",
]
