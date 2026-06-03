"""Per-day OCR-sentiment average — ``/stats/sentiment`` page + JSON API.

v1.6 feature 3/3. Renders a 30-day timeseries of the mean
``screenshots.sentiment`` value bucketed into UTC calendar days, plus
a JSON counterpart at ``/api/sentiment.json``.

Sentiment is produced by :mod:`app.ocr_sentiment` (a naive
lexicon-based scorer; see that module for the exact recipe) and
stored as a nullable ``REAL`` in ``[-1.0, +1.0]`` by migration 087.
``NULL`` rows fall out of the aggregate entirely — they mean
"never scored", *not* "scored as neutral". A shot with no lexicon
hits writes ``0.0`` explicitly and *does* contribute to the average.

The chart is a 480x180 SVG line, intentionally similar in dimensions
to :mod:`app.web.routes.ocr_length_chart` so the two stats panels
sit comfortably next to each other in the operator's mental layout.
The y-axis spans ``[-1, +1]`` with a zero baseline; the polyline is
clamped to that band on render.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, TypedDict

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["stats"])
log = get_logger("persona.ocr.sentiment")

# Window bounds. The default of 30 matches the task spec ("30-day
# average per day"); we still accept a ``?days=`` override within
# sane limits so an operator who wants a wider lens can ask for it.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 30

# Quick-select buttons rendered in the template. The exact 30 lives
# in the middle so the default highlights correctly.
_WINDOW_CHOICES: Final[tuple[int, ...]] = (7, 14, 30, 60, 90)

# SVG geometry. Same overall footprint as the OCR-length chart so the
# two panels can sit side by side without visual jitter.
_SVG_WIDTH: Final[int] = 480
_SVG_HEIGHT: Final[int] = 180
_PAD_LEFT: Final[int] = 42
_PAD_RIGHT: Final[int] = 16
_PAD_TOP: Final[int] = 14
_PAD_BOTTOM: Final[int] = 26

# Colours — green for the polyline (overall optimistic bias of the
# chart's existence on the dashboard), muted grey for the zero
# baseline, accent for hover dots.
_STROKE_LINE: Final[str] = "#34d399"  # emerald-400
_DOT_FILL: Final[str] = "#34d399"
_BASELINE_COLOUR: Final[str] = "#3f3f46"
_ZERO_LINE_COLOUR: Final[str] = "#71717a"
_AXIS_COLOUR: Final[str] = "#52525b"
_AXIS_TEXT: Final[str] = "#71717a"

# Fixed y-axis bounds — sentiment is already in ``[-1, +1]`` by
# construction, so we don't need a "nice ceiling" computation here.
_Y_MIN: Final[float] = -1.0
_Y_MAX: Final[float] = 1.0


class SentimentDay(TypedDict):
    """One day's bucket in the sentiment timeseries.

    Attributes:
        date:        ISO ``YYYY-MM-DD`` UTC calendar date.
        avg:         Mean ``sentiment`` over scored shots that day.
                     ``0.0`` when ``scored_count`` is zero so the
                     chart layer can multiply without a guard; the
                     ``scored_count`` field is the source of truth
                     for "did this day have any signal".
        scored_count: Number of shots on this day whose ``sentiment``
                     column is non-NULL (i.e. were actually scored).
    """

    date: str
    avg: float
    scored_count: int


def _day_key(captured_at: str) -> str | None:
    """Project an ISO timestamp onto its UTC ``YYYY-MM-DD`` calendar day.

    Mirrors the parsing strategy used elsewhere (see
    :func:`app.ocr_length_chart._day_key`) — SQLite's default
    ``datetime('now')`` shape is ``YYYY-MM-DD HH:MM:SS`` with no ``T``
    separator and no offset, which pre-3.11 ``fromisoformat`` rejects.
    The fallback slices the first 10 characters and validates them as
    a date so legacy rows still bucket correctly.

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


async def _daily_sentiment(days: int) -> list[SentimentDay]:
    """Aggregate per-day mean sentiment over the last ``days`` days.

    Walks every ``screenshots`` row with a non-NULL ``sentiment``
    value captured within the window, groups by UTC calendar day, and
    emits one :class:`SentimentDay` per day in the window (dense,
    oldest first). Days with zero scored shots emit ``avg=0.0`` and
    ``scored_count=0`` — the template and the chart projection both
    treat ``scored_count == 0`` as "no datum" and draw the polyline
    accordingly.
    """
    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=days - 1)
    cutoff_iso = datetime.combine(
        start_day, datetime.min.time(), tzinfo=UTC
    ).isoformat()

    sums: dict[str, float] = {}
    counts: dict[str, int] = {}

    rows_scanned = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT captured_at AS captured_at, sentiment AS sentiment "
            "FROM screenshots "
            "WHERE sentiment IS NOT NULL "
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
                    "ocr.sentiment.bad_captured_at_skipped",
                    captured_at=str(captured_at),
                )
                continue
            try:
                value = float(row["sentiment"])
            except (TypeError, ValueError):
                continue
            sums[day] = sums.get(day, 0.0) + value
            counts[day] = counts.get(day, 0) + 1

    buckets: list[SentimentDay] = []
    for offset in range(days):
        day = (start_day + timedelta(days=offset)).isoformat()
        scored = counts.get(day, 0)
        avg = round(sums.get(day, 0.0) / scored, 4) if scored > 0 else 0.0
        buckets.append(
            SentimentDay(date=day, avg=avg, scored_count=scored),
        )

    log.info(
        "ocr.sentiment.computed",
        days=days,
        rows_scanned=rows_scanned,
        non_empty_days=sum(1 for b in buckets if b["scored_count"] > 0),
    )
    return buckets


def _project_y(value: float, plot_top: float, plot_bottom: float) -> float:
    """Project a sentiment value in ``[-1, +1]`` onto SVG y-coordinates.

    SVG y grows downward, so the more positive the sentiment the
    smaller the y. Values are clamped to the band so a hypothetical
    out-of-range datum (shouldn't happen — :mod:`app.ocr_sentiment`
    clamps on its side) still lands inside the plot.
    """
    clamped = max(_Y_MIN, min(_Y_MAX, value))
    span = _Y_MAX - _Y_MIN
    plot_height = plot_bottom - plot_top
    return plot_bottom - ((clamped - _Y_MIN) / span) * plot_height


def _build_chart(buckets: list[SentimentDay]) -> dict[str, Any]:
    """Project the timeseries into SVG coordinates for the template.

    The chart skips days with ``scored_count == 0`` from the polyline
    so an empty stretch reads as a *gap* rather than a misleading
    straight line through ``0.0`` (which would imply "we scored these
    days and they came out neutral", which is not what happened).
    """
    plot_left = _PAD_LEFT
    plot_right = _SVG_WIDTH - _PAD_RIGHT
    plot_top = _PAD_TOP
    plot_bottom = _SVG_HEIGHT - _PAD_BOTTOM
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    point_count = len(buckets)
    x_step = plot_width / (point_count - 1) if point_count > 1 else 0.0

    # Every bucket gets a coordinate, even if it won't render as a dot
    # — we still need the x position for the x-axis labels.
    rendered_points: list[dict[str, Any]] = []
    polyline_segments: list[list[tuple[float, float]]] = []
    current_segment: list[tuple[float, float]] = []
    for idx, bucket in enumerate(buckets):
        x = (
            plot_left + idx * x_step
            if point_count > 1
            else plot_left + plot_width / 2
        )
        if bucket["scored_count"] > 0:
            y = _project_y(bucket["avg"], plot_top, plot_bottom)
            tooltip = (
                f"{bucket['date']} — "
                f"avg {bucket['avg']:+.3f} "
                f"over {bucket['scored_count']} shot"
                f"{'' if bucket['scored_count'] == 1 else 's'}"
            )
            rendered_points.append(
                {
                    "x": x,
                    "y": y,
                    "date": bucket["date"],
                    "avg": bucket["avg"],
                    "scored_count": bucket["scored_count"],
                    "fill": _DOT_FILL,
                    "tooltip": tooltip,
                }
            )
            current_segment.append((x, y))
        elif current_segment:
            # Break the polyline on empty days so the gap is visible.
            polyline_segments.append(current_segment)
            current_segment = []
    if current_segment:
        polyline_segments.append(current_segment)

    polylines = [
        " ".join(f"{x:.2f},{y:.2f}" for x, y in segment)
        for segment in polyline_segments
        if len(segment) >= 2
    ]

    # Y-axis ticks at -1, -0.5, 0, +0.5, +1 — sentiment is a fixed
    # bounded range so the ticks are constant.
    y_ticks: list[dict[str, Any]] = []
    for raw in (-1.0, -0.5, 0.0, 0.5, 1.0):
        ty = _project_y(raw, plot_top, plot_bottom)
        y_ticks.append({"y": ty, "label": f"{raw:+.1f}".replace("+0.0", "0.0")})

    # X-axis labels — ~6 evenly spaced ticks.
    x_labels: list[dict[str, Any]] = []
    desired_labels = 6
    if point_count > 0:
        stride = max(1, point_count // desired_labels)
        for idx, bucket in enumerate(buckets):
            if idx % stride != 0 and idx != point_count - 1:
                continue
            x = (
                plot_left + idx * x_step
                if point_count > 1
                else plot_left + plot_width / 2
            )
            # MM-DD form keeps the strip readable even at 90 days.
            short = bucket["date"][5:]
            x_labels.append({"x": x, "label": short})

    zero_y = _project_y(0.0, plot_top, plot_bottom)

    return {
        "width": _SVG_WIDTH,
        "height": _SVG_HEIGHT,
        "plot_left": plot_left,
        "plot_right": plot_right,
        "plot_top": plot_top,
        "plot_bottom": plot_bottom,
        "plot_width": plot_width,
        "plot_height": plot_height,
        "rendered_points": rendered_points,
        "polylines": polylines,
        "y_ticks": y_ticks,
        "x_labels": x_labels,
        "zero_y": zero_y,
        "stroke_line": _STROKE_LINE,
        "dot_fill": _DOT_FILL,
        "baseline_colour": _BASELINE_COLOUR,
        "zero_line_colour": _ZERO_LINE_COLOUR,
        "axis_colour": _AXIS_COLOUR,
        "axis_text": _AXIS_TEXT,
    }


def _summary(buckets: list[SentimentDay]) -> dict[str, Any]:
    """Compute headline numbers for the summary tiles."""
    total_scored = sum(b["scored_count"] for b in buckets)
    # Overall average — weighted by ``scored_count`` so a day with 200
    # shots dominates a day with 2 (which matches what the operator
    # actually wants: "what did this window *feel* like on average").
    weighted_sum = sum(b["avg"] * b["scored_count"] for b in buckets)
    overall_avg = (
        round(weighted_sum / total_scored, 4) if total_scored > 0 else 0.0
    )

    best: SentimentDay | None = None
    worst: SentimentDay | None = None
    for bucket in buckets:
        if bucket["scored_count"] <= 0:
            continue
        if best is None or bucket["avg"] > best["avg"]:
            best = bucket
        if worst is None or bucket["avg"] < worst["avg"]:
            worst = bucket

    non_empty_days = sum(1 for b in buckets if b["scored_count"] > 0)

    return {
        "total_scored": total_scored,
        "overall_avg": overall_avg,
        "best": best,
        "worst": worst,
        "non_empty_days": non_empty_days,
    }


@router.get("/stats/sentiment", response_class=HTMLResponse)
async def sentiment_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the per-day sentiment-average dashboard."""
    buckets = await _daily_sentiment(days=days)
    chart = _build_chart(buckets)
    summary = _summary(buckets)

    log.info(
        "ocr.sentiment.page",
        days=days,
        days_in_window=len(buckets),
        total_scored=summary["total_scored"],
        non_empty_days=summary["non_empty_days"],
    )

    return templates.TemplateResponse(
        request,
        "sentiment_stats.html",
        {
            "title": "OCR sentiment per day",
            "active_nav": "stats",
            "days": days,
            "min_days": _MIN_DAYS,
            "max_days": _MAX_DAYS,
            "window_choices": _WINDOW_CHOICES,
            "buckets": buckets,
            "chart": chart,
            "summary": summary,
        },
    )


@router.get("/api/sentiment.json", response_class=JSONResponse)
async def sentiment_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the raw per-day sentiment timeseries as JSON.

    Echoes the resolved ``days`` window plus the headline aggregate so
    a client can render its own summary tiles without re-walking the
    ``days_series`` array. ``best_day`` / ``worst_day`` are ``None``
    when the window has no scored shots at all.
    """
    buckets = await _daily_sentiment(days=days)
    summary = _summary(buckets)
    days_series: list[dict[str, Any]] = [
        {
            "date": bucket["date"],
            "avg": bucket["avg"],
            "scored_count": bucket["scored_count"],
        }
        for bucket in buckets
    ]
    best = summary["best"]
    worst = summary["worst"]
    payload: dict[str, Any] = {
        "days": days,
        "total_scored": summary["total_scored"],
        "overall_avg": summary["overall_avg"],
        "non_empty_days": summary["non_empty_days"],
        "best_day": dict(best) if best is not None else None,
        "worst_day": dict(worst) if worst is not None else None,
        "days_series": days_series,
    }
    return JSONResponse(payload)
