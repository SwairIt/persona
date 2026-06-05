"""Admin UI for the sleep-mode auto-detector (v1.62).

Settings + status page for the capture-loop hook in :mod:`app.sleep_mode`.

Routes:

    GET  /settings/sleep-mode              renders the toggle + threshold
                                           slider + recent transitions table
    POST /settings/sleep-mode              persists the form values and
                                           redirects (303) back to the page
    GET  /api/sleep-mode/events.json       JSON dump of the recent-events
                                           feed for external tooling

Mirrors the meeting-pause / privacy-mode admin shape so the three
capture short-circuits feel symmetric in the settings hub.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.capture import seconds_since_last_input
from app.logging_setup import get_logger
from app.sleep_mode import (
    IDLE_THRESHOLD_MINUTES_DEFAULT,
    KV_ENABLED,
    KV_THRESHOLD,
    recent_events,
    should_sleep,
)
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

log = get_logger("persona.sleep_mode.admin")

router = APIRouter(tags=["sleep-mode"])

# UI clamp on the threshold slider. Mirrors the clamp inside
# :mod:`app.sleep_mode` — keep the values in sync so a form post never
# silently snaps to a different value than what the slider showed.
_SLIDER_MIN_MINUTES: int = 1
_SLIDER_MAX_MINUTES: int = 240
_EVENTS_LIMIT: int = 50


def _parse_minutes(raw: str | None) -> int:
    """Parse the form's threshold value, clamping to the slider range.

    Lives in this module rather than reusing :mod:`app.sleep_mode`'s
    private clamp because the route also wants to validate against the
    SLIDER min/max (the worker's clamp is more permissive). Garbage
    collapses to the documented default.
    """
    if raw is None:
        return IDLE_THRESHOLD_MINUTES_DEFAULT
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return IDLE_THRESHOLD_MINUTES_DEFAULT
    if value < _SLIDER_MIN_MINUTES:
        return _SLIDER_MIN_MINUTES
    if value > _SLIDER_MAX_MINUTES:
        return _SLIDER_MAX_MINUTES
    return value


async def _read_current_settings() -> tuple[bool, int]:
    """Pull the two kv values, returning typed (enabled, minutes)."""
    async with get_connection() as conn:
        enabled_raw = await get_kv(conn, KV_ENABLED)
        threshold_raw = await get_kv(conn, KV_THRESHOLD)
    enabled = (enabled_raw or "0").strip() == "1"
    threshold = _parse_minutes(threshold_raw)
    return (enabled, threshold)


@router.get("/settings/sleep-mode", response_class=HTMLResponse)
async def sleep_mode_page(request: Request) -> HTMLResponse:
    """Render the sleep-mode settings + status page.

    Shows the on/off toggle, the threshold slider, the live "idle right
    now" readout (sampled via :func:`seconds_since_last_input`), and the
    most recent ``_EVENTS_LIMIT`` sleep/wake transitions.
    """
    enabled, threshold_minutes = await _read_current_settings()
    events = await recent_events(limit=_EVENTS_LIMIT)
    current_idle = float(seconds_since_last_input())
    decision = await should_sleep(current_idle)
    return templates.TemplateResponse(
        request,
        "sleep_mode.html",
        {
            "title": "Спящий режим",
            "active_nav": "settings",
            "enabled": enabled,
            "threshold_minutes": threshold_minutes,
            "threshold_default": IDLE_THRESHOLD_MINUTES_DEFAULT,
            "slider_min": _SLIDER_MIN_MINUTES,
            "slider_max": _SLIDER_MAX_MINUTES,
            "current_idle_seconds": int(current_idle),
            "currently_sleeping": bool(decision.get("sleeping")),
            "events": events,
        },
    )


@router.post("/settings/sleep-mode")
async def sleep_mode_save(
    enabled: str = Form(default=""),
    threshold_minutes: str = Form(default=""),
) -> RedirectResponse:
    """Persist the form values, then redirect (PRG) back to the page.

    HTML form-encoded checkboxes only POST their ``name`` when ticked,
    so the absence of ``enabled`` in the body means "off". The threshold
    is clamped to the slider range — see :func:`_parse_minutes`.
    """
    is_enabled = enabled.strip().lower() in {"1", "on", "true", "yes"}
    minutes = _parse_minutes(threshold_minutes)
    async with get_connection() as conn:
        await set_kv(conn, KV_ENABLED, "1" if is_enabled else "0")
        await set_kv(conn, KV_THRESHOLD, str(minutes))
    log.info(
        "sleep_mode.settings_saved",
        enabled=is_enabled,
        threshold_minutes=minutes,
    )
    return RedirectResponse(url="/settings/sleep-mode", status_code=303)


@router.get("/api/sleep-mode/events.json")
async def sleep_mode_events_json() -> JSONResponse:
    """Return the most recent sleep/wake transitions as JSON.

    Shape: ``{"events": [{id, occurred_at, state, idle_seconds}, ...]}``.
    Newest first. Used by external tooling that wants to graph the
    away-from-keyboard pattern without scraping the settings HTML.
    """
    events = await recent_events(limit=_EVENTS_LIMIT)
    return JSONResponse({"events": events})


__all__ = ["router"]
