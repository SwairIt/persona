"""Time-on-app dashboard — HTML page, JSON API, multi-day summary."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.time_on_app import app_summary, daily_time_on_app
from app.web.templates_engine import templates

log = get_logger("persona.time_on_app")

router = APIRouter(tags=["time-on-app"])


def _format_hms(seconds: int) -> str:
    """Format ``seconds`` as ``H:MM:SS`` (no external lib, divmod only)."""
    safe = max(int(seconds), 0)
    minutes, sec = divmod(safe, 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours}:{mins:02d}:{sec:02d}"


def _parse_day(value: str | None) -> date:
    """Parse a ``YYYY-MM-DD`` string; fall back to today on bad input."""
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            log.warning("time_on_app.bad_day_param", day=value)
    return datetime.now().astimezone().date()


def _decorate(
    items: list[dict[str, object]],
    total_seconds: int,
) -> list[dict[str, object]]:
    """Add ``duration`` (H:MM:SS) and ``percent`` (of max-app seconds) fields.

    Percent is computed against the *max* app's seconds (not the grand
    total) so the longest bar always fills 100 % of the row — that matches
    the spec's "CSS width = pct of max app's time".
    """
    if not items:
        return []
    max_sec = max((int(i["seconds"]) for i in items), default=0)  # type: ignore[call-overload]
    decorated: list[dict[str, object]] = []
    for item in items:
        sec = int(item["seconds"])  # type: ignore[call-overload]
        pct = (sec / max_sec * 100.0) if max_sec else 0.0
        share = (sec / total_seconds * 100.0) if total_seconds else 0.0
        decorated.append(
            {
                "app_name": item["app_name"],
                "seconds": sec,
                "shots": int(item["shots"]),  # type: ignore[call-overload]
                "duration": _format_hms(sec),
                "percent": pct,
                "share": share,
            }
        )
    return decorated


@router.get("/time-on-app", response_class=HTMLResponse)
async def time_on_app_page(
    request: Request,
    day: str | None = Query(default=None),
) -> HTMLResponse:
    target = _parse_day(day)
    raw_items = await daily_time_on_app(target.isoformat())
    total_seconds = sum(int(i["seconds"]) for i in raw_items)  # type: ignore[call-overload]
    items = _decorate(raw_items, total_seconds)
    return templates.TemplateResponse(
        request,
        "time_on_app.html",
        {
            "title": f"Time on app · {target.isoformat()}",
            "active_nav": "stats",
            "day": target,
            "day_iso": target.isoformat(),
            "prev_day": (target - timedelta(days=1)).isoformat(),
            "next_day": (target + timedelta(days=1)).isoformat(),
            "today_iso": datetime.now().astimezone().date().isoformat(),
            "items": items,
            "total_seconds": total_seconds,
            "total_duration": _format_hms(total_seconds),
        },
    )


@router.get("/api/time-on-app.json", response_class=JSONResponse)
async def time_on_app_json(
    day: str | None = Query(default=None),
) -> JSONResponse:
    target = _parse_day(day)
    items = await daily_time_on_app(target.isoformat())
    total_seconds = sum(int(i["seconds"]) for i in items)  # type: ignore[call-overload]
    return JSONResponse(
        {
            "day": target.isoformat(),
            "total_seconds": total_seconds,
            "total_duration": _format_hms(total_seconds),
            "items": items,
        }
    )


@router.get("/time-on-app/summary", response_class=HTMLResponse)
async def time_on_app_summary_page(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
) -> HTMLResponse:
    raw_items = await app_summary(days=days)
    total_seconds = sum(int(i["seconds"]) for i in raw_items)  # type: ignore[call-overload]
    items = _decorate(raw_items, total_seconds)
    today = datetime.now().astimezone().date()
    start_day = today - timedelta(days=days - 1)
    return templates.TemplateResponse(
        request,
        "time_on_app_summary.html",
        {
            "title": f"Time on app · last {days} days",
            "active_nav": "stats",
            "days": days,
            "start_day": start_day.isoformat(),
            "end_day": today.isoformat(),
            "items": items,
            "total_seconds": total_seconds,
            "total_duration": _format_hms(total_seconds),
        },
    )
