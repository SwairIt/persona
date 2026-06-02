"""Top-keywords-of-the-week — HTML tag-cloud page + JSON endpoint."""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.keywords import top_keywords
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.keywords")

router = APIRouter(tags=["keywords"])

# Allowed selector values surfaced in the form. Anything else is coerced
# to the nearest allowed bucket so handcrafted URLs can't generate huge
# look-back windows or unbounded result sets.
_ALLOWED_DAYS: tuple[int, ...] = (7, 30, 90)
_ALLOWED_N: tuple[int, ...] = (15, 30, 50)

# Tag-cloud font scale (rem) — clamps the log-scaled weight to a readable band.
_FONT_MIN_REM: float = 0.85
_FONT_MAX_REM: float = 2.40


def _snap(value: int, allowed: tuple[int, ...]) -> int:
    """Return the closest member of ``allowed`` to ``value``."""
    return min(allowed, key=lambda candidate: abs(candidate - value))


def _decorate(items: list[dict[str, int | str]]) -> list[dict[str, Any]]:
    """Attach pre-computed ``size_rem`` / ``weight`` / ``opacity`` per item.

    Jinja2 has no built-in ``log`` filter, so we precompute the visual
    weight in Python on a logarithmic scale and let the template just
    interpolate the numbers.
    """
    if not items:
        return []

    max_count = max(int(item["count"]) for item in items)
    log_max = math.log(max_count) if max_count > 1 else 1.0
    decorated: list[dict[str, Any]] = []
    for item in items:
        count = int(item["count"])
        ratio = (math.log(count) / log_max) if (max_count > 1 and count > 0) else 1.0
        ratio = max(0.0, min(1.0, ratio))
        size_rem = _FONT_MIN_REM + ratio * (_FONT_MAX_REM - _FONT_MIN_REM)
        weight = 400 + round(ratio * 5) * 100
        opacity = 0.55 + ratio * 0.45
        decorated.append(
            {
                "word": str(item["word"]),
                "count": count,
                "size_rem": round(size_rem, 2),
                "weight": int(weight),
                "opacity": round(opacity, 2),
            }
        )
    return decorated


@router.get("/keywords", response_class=HTMLResponse)
async def keywords_page(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
    n: int = Query(default=30, ge=1, le=200),
) -> HTMLResponse:
    days_choice = _snap(days, _ALLOWED_DAYS)
    n_choice = _snap(n, _ALLOWED_N)

    raw = await top_keywords(days=days_choice, top_n=n_choice)
    items = _decorate(raw)

    return templates.TemplateResponse(
        request,
        "keywords.html",
        {
            "title": "Top keywords",
            "active_nav": "stats",
            "items": items,
            "days": days_choice,
            "n": n_choice,
            "allowed_days": _ALLOWED_DAYS,
            "allowed_n": _ALLOWED_N,
        },
    )


@router.get("/api/keywords.json", response_class=JSONResponse)
async def keywords_json(
    days: int = Query(default=7, ge=1, le=365),
    n: int = Query(default=30, ge=1, le=200),
) -> JSONResponse:
    days_choice = _snap(days, _ALLOWED_DAYS)
    n_choice = _snap(n, _ALLOWED_N)
    items = await top_keywords(days=days_choice, top_n=n_choice)
    return JSONResponse(
        {
            "days": days_choice,
            "n": n_choice,
            "count": len(items),
            "items": items,
        }
    )
