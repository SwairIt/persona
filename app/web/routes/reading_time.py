"""Reading-time dashboard — HTML page + JSON API.

v0.48 feature 2/3. Wraps :func:`app.reading_time.reading_time_for_day`
in two endpoints:

* ``GET /stats/reading-time?day=YYYY-MM-DD`` — Tailwind page rendering
  the headline "N minutes at 250 wpm" figure, the OCR/notes split, and
  a per-app CSS bar chart.
* ``GET /api/reading-time.json?day=YYYY-MM-DD`` — JSON companion that
  returns the raw :class:`~app.reading_time.ReadingTimeResult`.

A missing or malformed ``day`` query parameter falls back to the local
calendar "today" — same convention as ``/time-on-app`` and
``/notes/day/{day}`` so the dashboard nav can deep-link without
constructing the date client-side.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main`; the v0.48 task spec forbids touching that
file. Wire it up in a follow-up patch with::

    from app.web.routes import reading_time as reading_time_routes
    app.include_router(reading_time_routes.router)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.reading_time import DEFAULT_WPM, reading_time_for_day
from app.web.templates_engine import templates

log = get_logger("persona.reading_time")

router = APIRouter(tags=["reading-time"])


def _today_local() -> date:
    """Local-date "today" — matches every other day-scoped view in the app."""
    return datetime.now().astimezone().date()


def _parse_day(value: str | None) -> date:
    """Parse a ``YYYY-MM-DD`` query value; fall back to local today.

    Forgiving on purpose: a stray digit in a copy-pasted URL should
    land the user on today's page rather than 400-ing. The bad value
    is logged for visibility.
    """
    if not value:
        return _today_local()
    try:
        return date.fromisoformat(value)
    except ValueError:
        log.info("reading_time.bad_day_param", day=value)
        return _today_local()


def _decorate_bars(by_app: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a ``percent`` field for the per-app CSS bar chart.

    Percent is computed against the *max* app's word count (not the
    grand total) so the longest bar always fills 100 % of the row —
    same visual convention as ``/time-on-app``.
    """
    if not by_app:
        return []
    max_words = max((int(item["words"]) for item in by_app), default=0)
    decorated: list[dict[str, Any]] = []
    for item in by_app:
        words = int(item["words"])
        pct = (words / max_words * 100.0) if max_words else 0.0
        decorated.append(
            {
                "app_name": str(item["app_name"]),
                "words": words,
                "percent": pct,
            }
        )
    return decorated


@router.get("/stats/reading-time", response_class=HTMLResponse)
async def reading_time_page(
    request: Request,
    day: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the per-day reading-time dashboard."""
    target = _parse_day(day)
    payload = await reading_time_for_day(target.isoformat())
    # ``payload["by_app"]`` is a list[TypedDict]; cast to the looser
    # ``dict[str, Any]`` shape the decorator + Jinja loop expect.
    by_app_raw: list[dict[str, Any]] = [
        {"app_name": item["app_name"], "words": item["words"]}
        for item in payload["by_app"]
    ]
    return templates.TemplateResponse(
        request,
        "reading_time.html",
        {
            "title": f"Reading time · {target.isoformat()}",
            "active_nav": "stats",
            "day_iso": target.isoformat(),
            "prev_day": (target - timedelta(days=1)).isoformat(),
            "next_day": (target + timedelta(days=1)).isoformat(),
            "today_iso": _today_local().isoformat(),
            "total_words_ocr": payload["total_words_ocr"],
            "total_words_notes": payload["total_words_notes"],
            "total_words": payload["total_words"],
            "minutes": payload["minutes_at_250wpm"],
            "wpm": payload["wpm"],
            "by_app": _decorate_bars(by_app_raw),
            "default_wpm": DEFAULT_WPM,
        },
    )


@router.get("/api/reading-time.json", response_class=JSONResponse)
async def reading_time_json(
    day: str | None = Query(default=None),
) -> JSONResponse:
    """Machine-readable companion to :func:`reading_time_page`.

    The shape mirrors :class:`~app.reading_time.ReadingTimeResult`
    exactly — the route layer adds no extra fields so downstream
    automation can rely on a single contract.
    """
    target = _parse_day(day)
    payload = await reading_time_for_day(target.isoformat())
    return JSONResponse(dict(payload))


__all__ = ["router"]
