"""OCR language statistics — `/stats/ocr-languages` HTML page + JSON API.

Renders the character-class breakdown produced by
:mod:`app.ocr.language_stats` as a CSS-only horizontal bar chart plus a
per-app table for the two dominant scripts.

A machine-readable counterpart lives at ``/api/ocr-languages.json``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.ocr.language_stats import language_breakdown
from app.web.templates_engine import templates

router = APIRouter(tags=["stats"])
log = get_logger("persona.ocr.lang_stats")

# Mirrors :data:`app.ocr.language_stats._MAX_DAYS` — duplicated here so
# FastAPI's query-parameter validator surfaces a 422 before the slow path
# even starts the SQLite scan.
_MIN_DAYS = 1
_MAX_DAYS = 365
_DEFAULT_DAYS = 30

# Display labels and Tailwind classes for each bucket. Order is also the
# rendering order of the bar chart, top-to-bottom.
_BUCKETS: tuple[tuple[str, str, str, str], ...] = (
    ("cyrillic", "cyrillic_chars", "Cyrillic", "bg-rose-500"),
    ("latin", "latin_chars", "Latin", "bg-sky-500"),
    ("cjk", "cjk_chars", "CJK", "bg-amber-500"),
    ("digit", "digit_chars", "Digits", "bg-emerald-500"),
    ("other", "other_chars", "Other", "bg-zinc-500"),
)


def _decorate(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the chart rows: label, count, percent, bar width, palette class.

    ``percent`` is rounded to one decimal place; ``width_pct`` is
    relative to the *largest* bucket (not the total) so even small
    minority scripts still get a visible bar — the absolute percentage
    is also printed next to the value for honesty.
    """
    counts: list[dict[str, Any]] = [
        {
            "bucket": bucket_key,
            "label": label,
            "count": int(data.get(count_key, 0)),
            "palette": palette,
        }
        for bucket_key, count_key, label, palette in _BUCKETS
    ]

    total = sum(row["count"] for row in counts)
    max_count = max((row["count"] for row in counts), default=0)

    decorated: list[dict[str, Any]] = []
    for row in counts:
        count = row["count"]
        percent = (count / total * 100.0) if total > 0 else 0.0
        width_pct = (count / max_count * 100.0) if max_count > 0 else 0.0
        decorated.append(
            {
                "bucket": row["bucket"],
                "label": row["label"],
                "count": count,
                "percent": round(percent, 1),
                "width_pct": round(width_pct, 1),
                "palette": row["palette"],
            }
        )
    return decorated


def _top_two_scripts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return chart rows for the two scripts with the most characters.

    Used to drive the per-app breakdown table: showing five scripts'
    worth of empty tables on a fresh install would be noise.
    """
    ranked = sorted(rows, key=lambda row: int(row["count"]), reverse=True)
    return [row for row in ranked if int(row["count"]) > 0][:2]


@router.get("/stats/ocr-languages", response_class=HTMLResponse)
async def ocr_language_stats_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the language-breakdown page."""
    data = await language_breakdown(days=days)
    chart_rows = _decorate(dict(data))
    top_two = _top_two_scripts(chart_rows)
    top_apps_by_language = data["top_apps_by_language"]

    breakdown_tables: list[dict[str, Any]] = []
    for row in top_two:
        breakdown_tables.append(
            {
                "label": row["label"],
                "bucket": row["bucket"],
                "palette": row["palette"],
                "apps": top_apps_by_language.get(row["bucket"], []),
            }
        )

    return templates.TemplateResponse(
        request,
        "ocr_language_stats.html",
        {
            "title": "OCR languages",
            "active_nav": "stats",
            "days": days,
            "chart_rows": chart_rows,
            "total_chars": data["total_chars"],
            "breakdown_tables": breakdown_tables,
            "min_days": _MIN_DAYS,
            "max_days": _MAX_DAYS,
        },
    )


@router.get("/api/ocr-languages.json", response_class=JSONResponse)
async def ocr_language_stats_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the raw breakdown as JSON.

    The payload is the :class:`~app.ocr.language_stats.LanguageBreakdown`
    dict verbatim plus the resolved ``days`` window the caller passed —
    handy when a client wants to display "stats for the last N days"
    without parroting the URL.
    """
    data = await language_breakdown(days=days)
    payload: dict[str, Any] = {"days": days, **dict(data)}
    return JSONResponse(payload)
