"""Unified capture settings page (v1.17).

Consolidates the controls a typical user cares about into one page:

- Master ON/OFF for screen capture and microphone
- Capture-rate slider mapped to ``capture_interval_seconds``
- Daily/weekly mic schedule

The page is intentionally a single column with large controls, so
nobody gets lost in a sea of toggles. Advanced knobs (per-app
overrides, retention tier widths, etc.) stay in the existing
``/settings`` page and the per-feature admin routes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["capture-settings"])
log = get_logger("persona.capture_settings")

_WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DEFAULT_DAYS = ",".join(_WEEKDAY_CODES)


async def _read_state() -> dict[str, object]:
    """Read every value the page needs in one pass."""
    cfg = get_settings()
    async with get_connection() as conn:
        screens_kill = await get_kv(conn, "capture_screens_disabled") or "0"
        mic_live_paused = await get_kv(conn, "audio_capture_paused_live") or "0"
        sched_enabled = await get_kv(conn, "mic_schedule_enabled") or "0"
        sched_days = await get_kv(conn, "mic_schedule_days") or _DEFAULT_DAYS
        sched_start = await get_kv(conn, "mic_schedule_start_hour") or "0"
        sched_end = await get_kv(conn, "mic_schedule_end_hour") or "24"
        capture_interval_kv = await get_kv(conn, "capture_interval_seconds_live")

    try:
        interval_seconds = (
            float(capture_interval_kv) if capture_interval_kv else cfg.capture_interval_seconds
        )
    except (TypeError, ValueError):
        interval_seconds = cfg.capture_interval_seconds

    selected_days = {d.strip() for d in sched_days.split(",") if d.strip()}
    return {
        "screens_disabled": screens_kill.strip() == "1",
        "mic_live_paused": mic_live_paused.strip() == "1",
        "mic_audio_capture_enabled": cfg.audio_capture_enabled,
        "schedule_enabled": sched_enabled.strip() == "1",
        "schedule_days": selected_days or set(_WEEKDAY_CODES),
        "schedule_start_hour": int(float(sched_start)),
        "schedule_end_hour": int(float(sched_end)),
        "interval_seconds": interval_seconds,
        "shots_per_minute": round(60.0 / max(interval_seconds, 0.5), 1),
        "daily_budget_mb": cfg.daily_budget_mb,
        "weekday_codes": _WEEKDAY_CODES,
    }


@router.get("/settings/capture", response_class=HTMLResponse)
async def capture_settings_page(request: Request) -> HTMLResponse:
    """Render the unified capture-settings panel."""
    state = await _read_state()
    return templates.TemplateResponse(
        request,
        "capture_settings.html",
        {
            "title": "Захват",
            "active_nav": "settings",
            **state,
        },
    )


@router.post("/settings/capture")
async def capture_settings_save(
    screens_enabled: Annotated[str, Form()] = "",
    interval_seconds: Annotated[float, Form()] = 6.0,
    mic_master_paused: Annotated[str, Form()] = "",
    schedule_enabled: Annotated[str, Form()] = "",
    schedule_days: Annotated[list[str] | None, Form()] = None,
    schedule_start_hour: Annotated[int, Form()] = 0,
    schedule_end_hour: Annotated[int, Form()] = 24,
) -> RedirectResponse:
    """Persist all values from the unified form in one go."""
    # Checkboxes only POST a value when checked, so the "off" state is
    # the absence of the field. We invert "screens_enabled" because the
    # kv flag stores the *disabled* state (so missing == disabled is
    # never the default behaviour).
    screens_disabled = "0" if screens_enabled == "on" else "1"
    mic_paused = "1" if mic_master_paused == "on" else "0"
    sched_on = "1" if schedule_enabled == "on" else "0"

    days_value = ",".join(
        d for d in (schedule_days or []) if d in _WEEKDAY_CODES
    ) or _DEFAULT_DAYS

    # Clamp the rate slider to the same bounds as Settings.capture_interval_seconds.
    interval_seconds = max(0.5, min(60.0, float(interval_seconds)))
    start_hour = max(0, min(24, int(schedule_start_hour)))
    end_hour = max(0, min(24, int(schedule_end_hour)))

    interval_str = f"{interval_seconds:.2f}"
    async with get_connection() as conn:
        await set_kv(conn, "capture_screens_disabled", screens_disabled)
        await set_kv(conn, "audio_capture_paused_live", mic_paused)
        await set_kv(conn, "mic_schedule_enabled", sched_on)
        await set_kv(conn, "mic_schedule_days", days_value)
        await set_kv(conn, "mic_schedule_start_hour", str(start_hour))
        await set_kv(conn, "mic_schedule_end_hour", str(end_hour))
        # v1.25 — write BOTH the canonical kv key and the legacy ``_live``
        # alias so any reader (capture_loop, setup wizard, future code)
        # gets the same value. Once all callers route through
        # ``app.settings.effective`` the ``_live`` alias can go away.
        await set_kv(conn, "capture_interval_seconds", interval_str)
        await set_kv(conn, "capture_interval_seconds_live", interval_str)

    log.info(
        "capture_settings.saved",
        screens_disabled=screens_disabled,
        mic_paused=mic_paused,
        schedule_on=sched_on,
        days=days_value,
        start=start_hour,
        end=end_hour,
        interval=interval_seconds,
    )
    return RedirectResponse(url="/settings/capture", status_code=303)


@router.get("/api/settings/capture")
async def capture_settings_json() -> JSONResponse:
    """JSON sibling — useful for the dashboard / external scripts."""
    state = await _read_state()
    state["schedule_days"] = sorted(state["schedule_days"])  # JSON-friendly
    return JSONResponse(state)


__all__ = ["router"]
