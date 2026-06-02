"""Idle-time stats dashboard — HTML page + JSON API.

The page shows two big H:MM:SS counters (active vs idle), a single-bar
ratio visualisation, the day's first/last capture timestamps, shot
counts and a day picker.  Logic lives in :mod:`app.idle_stats`; this
module is a thin presentation shell.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.idle_stats import (
    DEFAULT_IDLE_THRESHOLD_S,
    DEFAULT_MAX_GAP_S,
    daily_idle,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.idle")

router = APIRouter(tags=["idle-stats"])


def _format_hms(seconds: int) -> str:
    """Format ``seconds`` as ``H:MM:SS`` (divmod, no external lib)."""
    safe = max(int(seconds), 0)
    minutes, sec = divmod(safe, 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours}:{mins:02d}:{sec:02d}"


def _parse_day(value: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to today on bad / missing input."""
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            log.warning("idle.bad_day_param", day=value)
    return datetime.now().astimezone().date()


def _format_capture(iso_value: str | None) -> str:
    """Render a stored ISO timestamp as ``HH:MM:SS`` (or em-dash)."""
    if iso_value is None:
        return "—"
    try:
        return datetime.fromisoformat(iso_value).strftime("%H:%M:%S")
    except ValueError:
        return iso_value


@router.get("/idle", response_class=HTMLResponse)
async def idle_page(
    request: Request,
    day: str | None = Query(default=None),
    threshold: int = Query(
        default=DEFAULT_IDLE_THRESHOLD_S, ge=1, le=86_400,
    ),
) -> HTMLResponse:
    target = _parse_day(day)
    stats = await daily_idle(
        target.isoformat(),
        idle_threshold_s=threshold,
        max_gap_s=DEFAULT_MAX_GAP_S,
    )

    active_secs = stats["active_seconds"]
    idle_secs = stats["idle_seconds"]
    total_secs = active_secs + idle_secs

    active_pct = (active_secs / total_secs * 100.0) if total_secs else 0.0
    idle_pct = (idle_secs / total_secs * 100.0) if total_secs else 0.0

    return templates.TemplateResponse(
        request,
        "idle_stats.html",
        {
            "title": f"Idle stats · {target.isoformat()}",
            "active_nav": "stats",
            "day": target,
            "day_iso": target.isoformat(),
            "prev_day": (target - timedelta(days=1)).isoformat(),
            "next_day": (target + timedelta(days=1)).isoformat(),
            "today_iso": datetime.now().astimezone().date().isoformat(),
            "threshold": threshold,
            "active_seconds": active_secs,
            "idle_seconds": idle_secs,
            "total_seconds": total_secs,
            "active_duration": _format_hms(active_secs),
            "idle_duration": _format_hms(idle_secs),
            "total_duration": _format_hms(total_secs),
            "active_shots": stats["active_shots"],
            "idle_shots": stats["idle_shots"],
            "active_percent": active_pct,
            "idle_percent": idle_pct,
            "first_capture_iso": stats["first_capture"],
            "last_capture_iso": stats["last_capture"],
            "first_capture": _format_capture(stats["first_capture"]),
            "last_capture": _format_capture(stats["last_capture"]),
        },
    )


@router.get("/api/idle.json", response_class=JSONResponse)
async def idle_json(
    day: str | None = Query(default=None),
    threshold: int = Query(
        default=DEFAULT_IDLE_THRESHOLD_S, ge=1, le=86_400,
    ),
) -> JSONResponse:
    target = _parse_day(day)
    stats = await daily_idle(
        target.isoformat(),
        idle_threshold_s=threshold,
        max_gap_s=DEFAULT_MAX_GAP_S,
    )
    payload: dict[str, object] = dict(stats)
    payload["active_duration"] = _format_hms(stats["active_seconds"])
    payload["idle_duration"] = _format_hms(stats["idle_seconds"])
    payload["threshold_seconds"] = threshold
    return JSONResponse(payload)
