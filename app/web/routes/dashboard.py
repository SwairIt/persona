"""Unified dashboard — single-page roll-up of the most-useful stats.

Pulls together the existing streak/controller/digest/screenshot surfaces
into one card grid with minimal-chrome SVG sparklines. The route never
introduces a new SQL helper — it only composes the same building blocks
the rest of the web layer already uses (``current_streak``,
``get_controller``, ``get_connection``).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.streak import current_streak
from app.web.routes.dashboard_tiles import load_tile_order
from app.web.routes.dashboard_widgets import COUNT_CAP, collect_widgets
from app.web.templates_engine import templates
from app.workers.control import get_controller

log = get_logger("persona.dashboard")

router = APIRouter(tags=["dashboard"])

_SHOTS_WINDOW_DAYS = 7
_TOP_APPS_LIMIT = 5


def _seven_day_axis(today: date) -> list[str]:
    """Return the last 7 ISO calendar days, oldest-first, ending on ``today``."""
    span = range(_SHOTS_WINDOW_DAYS - 1, -1, -1)
    return [(today - timedelta(days=offset)).isoformat() for offset in span]


async def _collect_dashboard() -> dict[str, Any]:
    """Aggregate every card's payload into a single dict — used by both views."""
    today = date.today()
    axis = _seven_day_axis(today)

    streak_payload = await current_streak()
    controller = get_controller()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "AND captured_at >= DATE('now', ?) "
            "GROUP BY day",
            (f"-{_SHOTS_WINDOW_DAYS - 1} days",),
        )
        shots_rows = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "AND captured_at >= DATE('now', ?) "
            "AND app_name IS NOT NULL "
            "GROUP BY app_name ORDER BY n DESC LIMIT ?",
            (f"-{_SHOTS_WINDOW_DAYS - 1} days", _TOP_APPS_LIMIT),
        )
        top_apps_rows = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT week_start, provider, generated_at "
            "FROM weekly_digest ORDER BY week_start DESC LIMIT 1"
        )
        digest_row = await cursor.fetchone()

    shots_by_day: dict[str, int] = {str(row["day"]): int(row["n"]) for row in shots_rows}
    shots_series: list[int] = [shots_by_day.get(day, 0) for day in axis]
    shots_week_total = sum(shots_series)
    today_count = int(streak_payload["today_count"])

    top_apps: list[dict[str, Any]] = [
        {"app": str(row["app_name"]), "count": int(row["n"])} for row in top_apps_rows
    ]

    if digest_row is None:
        latest_digest: dict[str, Any] | None = None
    else:
        week_start_iso = str(digest_row["week_start"])
        raw_provider = digest_row["provider"]
        provider = str(raw_provider) if raw_provider is not None else None
        latest_digest = {
            "week_start": week_start_iso,
            "title": f"Week of {week_start_iso}",
            "provider": provider,
            "generated_at": str(digest_row["generated_at"]),
        }

    capture_state = {
        "paused": bool(controller.paused),
        "stopped": controller.stop_event.is_set(),
        "captures_total": int(controller.captures_total),
        "captures_failed": int(controller.captures_failed),
        "last_capture_at": (
            controller.last_capture_at.isoformat() if controller.last_capture_at else None
        ),
    }

    log.info(
        "dashboard.computed",
        today_count=today_count,
        streak_days=int(streak_payload["days"]),
        shots_week_total=shots_week_total,
        top_apps=len(top_apps),
        digest_present=latest_digest is not None,
        capture_paused=capture_state["paused"],
    )

    return {
        "today": {
            "count": today_count,
            "axis": axis,
            "series": shots_series,
            "week_total": shots_week_total,
        },
        "streak": {
            "days": int(streak_payload["days"]),
            "longest": int(streak_payload["longest"]),
            "last_capture_date": streak_payload["last_capture_date"],
        },
        "top_apps": top_apps,
        "latest_digest": latest_digest,
        "capture": capture_state,
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> HTMLResponse:
    payload = await _collect_dashboard()
    # v0.81 — tile order comes from kv_settings via /settings/dashboard.
    # ``load_tile_order`` already filters against the server-side
    # whitelist + appends any newly-shipped tiles, so the template can
    # iterate it directly without re-validating each name.
    tile_order = await load_tile_order()
    # v0.86 — render any user-defined widgets after the built-in tiles.
    # ``collect_widgets`` runs each saved query live and swallows
    # per-widget errors so a broken row can't 500 the whole page.
    widgets = await collect_widgets()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "title": "Dashboard",
            "active_nav": "stats",
            "tile_order": tile_order,
            "widgets": widgets,
            "widget_count_cap": COUNT_CAP,
            **payload,
        },
    )


@router.get("/api/dashboard.json", response_class=JSONResponse)
async def dashboard_json() -> JSONResponse:
    payload = await _collect_dashboard()
    widgets = await collect_widgets()
    payload["widgets"] = widgets
    return JSONResponse(payload)
