"""Settings + test endpoints for the memory-of-the-day morning push.

Three operator-facing surfaces:

* ``GET  /settings/memory-of-day`` — full HTML form (extends ``base.html``)
  with the hour-of-day picker and the enable/disable toggle.
* ``POST /settings/memory-of-day`` — persists the form via ``kv_settings``
  and PRG-redirects back to the same page.
* ``POST /api/memory-of-day/test`` — fires one off-schedule pick + push so
  the operator can see exactly what the morning notification will look
  like without waiting for the configured hour.

The kv keys this route reads/writes are the same ones
:mod:`app.workers.memory_of_day_worker` consults — editing either side
means editing both.

The default enabled state is *on* (the picker is read-only and cheap), and
the default hour is ``9``. Form input is validated server-side so a
hand-crafted POST cannot park the hour at ``42``.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import notifications
from app.logging_setup import get_logger
from app.memory_of_day import MemoryPick, pick_memory
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["memory-of-day"])
log = get_logger("persona.web.memory_of_day_settings")

#: kv rows shared with :mod:`app.workers.memory_of_day_worker`. Touch both
#: at once when renaming.
_KV_HOUR: str = "memory_of_day_hour_local"
_KV_ENABLED: str = "memory_of_day_enabled"

_DEFAULT_HOUR: int = 9
_DEFAULT_ENABLED: bool = True


def _coerce_hour(raw: str | None) -> int:
    """Parse the stored hour; fall back to ``_DEFAULT_HOUR`` on any bad shape."""
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return _DEFAULT_HOUR
    return value if 0 <= value <= 23 else _DEFAULT_HOUR


def _coerce_enabled(raw: str | None) -> bool:
    """Parse the stored enabled flag.

    ``None`` keeps the default-on behaviour. Once the row exists it must
    be the literal ``"1"`` to count as enabled — anything else is off,
    matching the worker's own getter.
    """
    if raw is None:
        return _DEFAULT_ENABLED
    return raw.strip() == "1"


@router.get("/settings/memory-of-day", response_class=HTMLResponse)
async def memory_of_day_settings_page(request: Request) -> HTMLResponse:
    """Render the operator settings page (hour + enabled toggle)."""
    async with get_connection() as conn:
        raw_hour = await get_kv(conn, _KV_HOUR)
        raw_enabled = await get_kv(conn, _KV_ENABLED)
    hour = _coerce_hour(raw_hour)
    enabled = _coerce_enabled(raw_enabled)
    return templates.TemplateResponse(
        request,
        "memory_of_day_settings.html",
        {
            "title": "Память дня",
            "active_nav": "settings",
            "hour": hour,
            "enabled": enabled,
        },
    )


@router.post("/settings/memory-of-day")
async def memory_of_day_settings_save(
    hour: int = Form(...),
    enabled: str = Form("off"),
) -> RedirectResponse:
    """Persist the hour + enabled toggle, then PRG back to the form."""
    if not 0 <= hour <= 23:
        raise HTTPException(status_code=400, detail="hour must be 0..23")
    is_on = enabled.strip().lower() in {"on", "1", "true", "yes"}
    async with get_connection() as conn:
        await set_kv(conn, _KV_HOUR, str(hour))
        await set_kv(conn, _KV_ENABLED, "1" if is_on else "0")
    log.info("memory_of_day.settings.saved", hour=hour, enabled=is_on)
    return RedirectResponse(url="/settings/memory-of-day", status_code=303)


def _build_title(pick: MemoryPick) -> str:
    """Notification headline — mirrors the worker so test == real."""
    years = int(pick["years_back"])
    prefix = "1 год назад" if years == 1 else f"{years} года назад"
    kind = pick["kind"]
    if kind == "pinned_shot":
        return f"{prefix} — закреплённый момент"
    if kind == "daily_pin":
        return f"{prefix} — итог дня"
    return f"{prefix} — момент из этого дня"


def _build_link(pick: MemoryPick) -> str:
    """Deep-link target — kept in sync with the worker."""
    kind = pick["kind"]
    if kind in {"pinned_shot", "random_shot"}:
        return f"/screenshot/{int(pick['shot_id'])}"
    return "/memory/replay"


@router.post("/api/memory-of-day/test")
async def memory_of_day_test() -> JSONResponse:
    """Fire one off-schedule pick + push.

    Used by the "Show me what it'll look like" button on the settings
    page. Returns:

    * ``{ok: true, pushed: true,  notif_id, kind, years_back, date}`` when
      the picker found a memory and the notification was inserted.
    * ``{ok: true, pushed: false, reason: "no_memory"}`` when the picker
      returned ``None`` (empty corpus / no anniversary signal).
    * ``HTTP 500`` when the push itself fails — the operator should see
      a loud error instead of a silent green checkmark.
    """
    pick = await pick_memory()
    if pick is None:
        log.info("memory_of_day.test.no_memory")
        return JSONResponse({"ok": True, "pushed": False, "reason": "no_memory"})

    title = _build_title(pick)
    body = pick.get("summary") or ""
    link = _build_link(pick)
    notif_id = await notifications.push(
        kind="memory-of-day",
        title=title,
        body=body or None,
        link=link,
        severity="info",
    )
    log.info(
        "memory_of_day.test.pushed",
        notif_id=notif_id,
        kind=pick["kind"],
        years_back=int(pick["years_back"]),
    )
    return JSONResponse(
        {
            "ok": True,
            "pushed": True,
            "notif_id": notif_id,
            "kind": pick["kind"],
            "years_back": int(pick["years_back"]),
            "date": pick["date_iso"],
        }
    )


__all__ = ["router"]
