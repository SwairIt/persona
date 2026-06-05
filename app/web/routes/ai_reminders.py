"""Routes for the AI-suggested daily reminders feature (v1.46).

Three surfaces:

* ``GET  /reminders/ai``                — full HTML list of undismissed
                                          suggestions with Dismiss /
                                          Snooze buttons.
* ``POST /api/ai-reminders/{id}/dismiss`` — soft-delete one row.
* ``POST /api/ai-reminders/{id}/snooze``  body ``{hours: int}`` —
                                            push the row's ``due_at``
                                            into the future without
                                            dismissing it.
* ``GET  /api/ai-reminders/today.json`` — JSON list for the dashboard
                                          widget / TG-bot polling.
* ``GET  /settings/ai-reminders``       — operator config (hour +
                                          enabled toggle); driven by
                                          the same kv rows the worker
                                          reads.

The worker writes new rows; this module only reads / mutates existing
ones. All SQL is parametrised. Mutation endpoints return JSON
(``{ok, id}``) so the dashboard widget can call them via ``fetch`` and
re-render without a full page reload.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.storage.time import iso
from app.web.templates_engine import templates

router = APIRouter(tags=["ai-reminders"])

log = get_logger("persona.web.ai_reminders")

#: kv rows shared with :mod:`app.workers.ai_reminders_worker`. Editing
#: either name here means editing the worker too.
_KV_HOUR: str = "ai_reminders_hour_local"
_KV_ENABLED: str = "ai_reminders_enabled"
_DEFAULT_HOUR: int = 22

#: Cap accepted by the snooze endpoint. A week is plenty for "remind me
#: later"; values above it almost certainly indicate a UI bug.
_SNOOZE_HOURS_MIN: int = 1
_SNOOZE_HOURS_MAX: int = 168


class _SnoozePayload(BaseModel):
    """JSON body for the snooze endpoint."""

    hours: int = Field(..., ge=_SNOOZE_HOURS_MIN, le=_SNOOZE_HOURS_MAX)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an aiosqlite Row into a plain serialisable dict."""
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "source_day": str(row["source_day"]),
        "title": str(row["title"]),
        "body": str(row["body"]) if row["body"] is not None else None,
        "severity": str(row["severity"]),
        "due_at": str(row["due_at"]) if row["due_at"] is not None else None,
        "dismissed_at": (
            str(row["dismissed_at"]) if row["dismissed_at"] is not None else None
        ),
    }


@router.get("/reminders/ai", response_class=HTMLResponse)
async def ai_reminders_page(request: Request) -> HTMLResponse:
    """Render the list of undismissed AI reminders."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, created_at, source_day, title, body, severity, "
            "due_at, dismissed_at FROM ai_reminder "
            "WHERE dismissed_at IS NULL "
            "ORDER BY "
            "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, "
            "due_at ASC, created_at DESC"
        )
        rows = await cursor.fetchall()
    items = [_row_to_dict(row) for row in rows]
    log.info("ai_reminders.page", count=len(items))
    return templates.TemplateResponse(
        request,
        "ai_reminders.html",
        {
            "title": "AI напоминания",
            "active_nav": "reminders",
            "items": items,
        },
    )


@router.post("/api/ai-reminders/{reminder_id}/dismiss")
async def ai_reminders_dismiss(reminder_id: int) -> JSONResponse:
    """Soft-delete one reminder. Returns 404 if no such row exists."""
    now_iso = iso(datetime.now(UTC))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE ai_reminder SET dismissed_at = ? "
            "WHERE id = ? AND dismissed_at IS NULL",
            (now_iso, reminder_id),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Reminder not found")
    log.info("ai_reminders.dismiss", id=reminder_id)
    return JSONResponse({"ok": True, "id": reminder_id})


@router.post("/api/ai-reminders/{reminder_id}/snooze")
async def ai_reminders_snooze(
    reminder_id: int, payload: _SnoozePayload
) -> JSONResponse:
    """Push the row's ``due_at`` forward by ``hours`` (1..168).

    Snoozing a row with no prior ``due_at`` anchors the new time to
    "now" — the most useful interpretation when the user clicks
    "remind me in 3h" on a freshly-suggested untimed item.
    """
    new_due = datetime.now(UTC) + timedelta(hours=payload.hours)
    new_due_iso = iso(new_due)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE ai_reminder SET due_at = ? "
            "WHERE id = ? AND dismissed_at IS NULL",
            (new_due_iso, reminder_id),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Reminder not found")
    log.info(
        "ai_reminders.snooze", id=reminder_id, hours=payload.hours, due_at=new_due_iso
    )
    return JSONResponse(
        {"ok": True, "id": reminder_id, "due_at": new_due_iso}
    )


@router.get("/api/ai-reminders/today.json")
async def ai_reminders_today_json() -> JSONResponse:
    """Return today's undismissed reminders as JSON.

    "Today" means rows whose ``source_day`` equals the local-clock
    calendar date — i.e. the most recent batch the worker produced.
    Older un-dismissed rows are still on the HTML page but not in this
    feed so the dashboard widget doesn't grow unboundedly.
    """
    today_iso = datetime.now().astimezone().date().isoformat()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, created_at, source_day, title, body, severity, "
            "due_at, dismissed_at FROM ai_reminder "
            "WHERE dismissed_at IS NULL AND source_day = ? "
            "ORDER BY "
            "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, "
            "due_at ASC, created_at DESC",
            (today_iso,),
        )
        rows = await cursor.fetchall()
    items = [_row_to_dict(row) for row in rows]
    return JSONResponse({"ok": True, "source_day": today_iso, "items": items})


@router.get("/settings/ai-reminders", response_class=HTMLResponse)
async def ai_reminders_settings_page(request: Request) -> HTMLResponse:
    """Render the operator settings page (hour + enabled toggle)."""
    async with get_connection() as conn:
        raw_hour = await get_kv(conn, _KV_HOUR)
        raw_enabled = await get_kv(conn, _KV_ENABLED)
    hour = _coerce_hour(raw_hour)
    enabled = (raw_enabled or "0").strip() == "1"
    return templates.TemplateResponse(
        request,
        "ai_reminders_settings.html",
        {
            "title": "AI напоминания — настройки",
            "active_nav": "settings",
            "hour": hour,
            "enabled": enabled,
        },
    )


@router.post("/settings/ai-reminders")
async def ai_reminders_settings_save(
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
    log.info("ai_reminders.settings.saved", hour=hour, enabled=is_on)
    return RedirectResponse(url="/settings/ai-reminders", status_code=303)


def _coerce_hour(raw: str | None) -> int:
    """Best-effort parse of the stored hour; fall back to the default."""
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return _DEFAULT_HOUR
    return value if 0 <= value <= 23 else _DEFAULT_HOUR


__all__ = ["router"]
