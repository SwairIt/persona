"""Personal-metrics dashboard — HTML page + JSON API.

v1.5 feature 3/3 surfaces the lifetime KPI snapshot produced by
:func:`app.personal_metrics.compute_metrics` under two URLs:

* ``GET /stats/personal`` renders a Tailwind 6-card grid (one card per
  metric) using the shared :file:`base.html` chrome.
* ``GET /api/personal-metrics.json`` returns the same snapshot as JSON
  for scripting and the eventual auto-refresh layer.

This module is a thin presentation shell — every count lives in
:mod:`app.personal_metrics`. The route only formats numbers for the
template (thousand-separators, em-dashes are unnecessary because the
snapshot is integer-only) and never touches the DB directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.personal_metrics import PersonalMetrics, compute_metrics
from app.web.templates_engine import templates

log = get_logger("persona.personal_metrics")

router = APIRouter(tags=["personal-metrics"])


def _format_thousands(value: int) -> str:
    """Render ``value`` with ASCII thousands separators (``1,234,567``).

    Kept in the route layer rather than the module surface so JSON
    consumers still get a raw ``int`` — only the HTML render needs the
    human-friendly grouping.
    """
    return f"{int(value):,}"


def _build_context(metrics: PersonalMetrics) -> dict[str, object]:
    """Pack the snapshot + presentation strings for the template."""
    return {
        "title": "Personal metrics",
        "active_nav": "stats",
        "lifetime_shots": metrics["lifetime_shots"],
        "lifetime_shots_display": _format_thousands(metrics["lifetime_shots"]),
        "lifetime_distinct_apps": metrics["lifetime_distinct_apps"],
        "lifetime_distinct_apps_display": _format_thousands(
            metrics["lifetime_distinct_apps"]
        ),
        "longest_streak": metrics["longest_streak"],
        "longest_streak_display": _format_thousands(metrics["longest_streak"]),
        "total_ocr_chars": metrics["total_ocr_chars"],
        "total_ocr_chars_display": _format_thousands(metrics["total_ocr_chars"]),
        "total_notes": metrics["total_notes"],
        "total_notes_display": _format_thousands(metrics["total_notes"]),
        "total_annotations": metrics["total_annotations"],
        "total_annotations_display": _format_thousands(metrics["total_annotations"]),
    }


@router.get("/stats/personal", response_class=HTMLResponse)
async def personal_metrics_page(request: Request) -> HTMLResponse:
    """Render the six-card Tailwind page."""
    metrics = await compute_metrics()
    log.info("personal_metrics.page.rendered")
    return templates.TemplateResponse(
        request,
        "personal_metrics.html",
        _build_context(metrics),
    )


@router.get("/api/personal-metrics.json", response_class=JSONResponse)
async def personal_metrics_json() -> JSONResponse:
    """Return the snapshot as JSON for scripting and auto-refresh."""
    metrics = await compute_metrics()
    log.info("personal_metrics.json.served")
    # ``PersonalMetrics`` is a ``TypedDict`` of plain ints; copy into a
    # fresh ``dict[str, object]`` so the response annotation matches
    # what ``JSONResponse`` expects (FastAPI's serialiser is happy with
    # the ``TypedDict`` too, but the explicit dict makes the contract
    # legible at the call site).
    payload: dict[str, object] = {
        "lifetime_shots": metrics["lifetime_shots"],
        "lifetime_distinct_apps": metrics["lifetime_distinct_apps"],
        "longest_streak": metrics["longest_streak"],
        "total_ocr_chars": metrics["total_ocr_chars"],
        "total_notes": metrics["total_notes"],
        "total_annotations": metrics["total_annotations"],
    }
    return JSONResponse(payload)
