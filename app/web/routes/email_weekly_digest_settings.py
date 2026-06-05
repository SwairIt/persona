"""Settings + preview endpoints for the weekly email digest (v1.59).

Four surfaces, all rooted at ``/settings/email-weekly-digest`` and
``/api/email-weekly-digest``:

* ``GET  /settings/email-weekly-digest``        — HTML form (enabled + hour).
* ``POST /settings/email-weekly-digest``        — persist the form.
* ``POST /api/email-weekly-digest/send-now``    — fire one immediate send
                                                    via the existing SMTP
                                                    helper; returns the raw
                                                    status dict so the UI
                                                    can paint sent /
                                                    misconfigured /
                                                    missing_dep / error.
* ``GET  /api/email-weekly-digest/preview.html`` — render the digest body
                                                    for the *current* ISO
                                                    week so the operator
                                                    can eyeball it in a
                                                    browser tab without
                                                    actually shipping mail.

Shares the same kv rows the worker reads (``email_weekly_digest_enabled``,
``email_weekly_digest_hour_local``); renaming either here means renaming
the worker too.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.email_weekly_digest import build_weekly_digest_body
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates
from app.workers.email_weekly_digest_worker import send_weekly_digest_now

router = APIRouter(tags=["email-weekly-digest"])
log = get_logger("persona.web.email_weekly_digest_settings")

# kv rows shared with :mod:`app.workers.email_weekly_digest_worker`.
_KV_HOUR: str = "email_weekly_digest_hour_local"
_KV_ENABLED: str = "email_weekly_digest_enabled"
_DEFAULT_HOUR: int = 19


def _coerce_hour(raw: str | None) -> int:
    """Best-effort parse of the stored hour; fall back to the default."""
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return _DEFAULT_HOUR
    return value if 0 <= value <= 23 else _DEFAULT_HOUR


def _current_week_start_iso() -> str:
    """Return the Monday of the current local ISO week, ``YYYY-MM-DD``."""
    today = datetime.now().astimezone().date()
    return (today - timedelta(days=today.weekday())).isoformat()


@router.get("/settings/email-weekly-digest", response_class=HTMLResponse)
async def email_weekly_digest_settings_page(request: Request) -> HTMLResponse:
    """Render the operator settings form."""
    async with get_connection() as conn:
        raw_hour = await get_kv(conn, _KV_HOUR)
        raw_enabled = await get_kv(conn, _KV_ENABLED)
    hour = _coerce_hour(raw_hour)
    enabled = (raw_enabled or "0").strip() == "1"
    week_start_iso = _current_week_start_iso()
    return templates.TemplateResponse(
        request,
        "email_weekly_digest_settings.html",
        {
            "title": "Еженедельный дайджест",
            "active_nav": "settings",
            "hour": hour,
            "enabled": enabled,
            "week_start_iso": week_start_iso,
        },
    )


@router.post("/settings/email-weekly-digest")
async def email_weekly_digest_settings_save(
    hour: int = Form(...),
    enabled: str = Form("off"),
) -> RedirectResponse:
    """Persist the two kv rows and PRG back to the form."""
    if not 0 <= hour <= 23:
        raise HTTPException(status_code=400, detail="hour must be 0..23")
    is_on = enabled.strip().lower() in {"on", "1", "true", "yes"}
    async with get_connection() as conn:
        await set_kv(conn, _KV_HOUR, str(hour))
        await set_kv(conn, _KV_ENABLED, "1" if is_on else "0")
    log.info(
        "email_weekly_digest.settings.saved",
        hour=hour,
        enabled=is_on,
    )
    return RedirectResponse(
        url="/settings/email-weekly-digest", status_code=303
    )


@router.post("/api/email-weekly-digest/send-now")
async def email_weekly_digest_send_now() -> JSONResponse:
    """Fire one immediate send for testing.

    Returns the raw status dict from :func:`send_digest_email` so the
    UI can render either ``sent`` / ``disabled`` / ``misconfigured``
    / ``missing_dep`` / ``error`` without translating it twice. HTTP
    status is always 200 — the *payload* tells the front-end what to do.
    """
    result = await send_weekly_digest_now()
    log.info(
        "email_weekly_digest.send_now.result",
        status=result.get("status"),
    )
    return JSONResponse(result)


@router.get(
    "/api/email-weekly-digest/preview.html", response_class=HTMLResponse
)
async def email_weekly_digest_preview() -> HTMLResponse:
    """Return the digest body for the current week as an HTML page.

    Useful for eyeballing typography + thumbnail layout without
    actually sending the message. The response is the same string
    :func:`send_weekly_digest_now` ships to SMTP.
    """
    week_start_iso = _current_week_start_iso()
    body_html = await build_weekly_digest_body(week_start_iso)
    log.info(
        "email_weekly_digest.preview",
        week_start=week_start_iso,
        body_bytes=len(body_html.encode("utf-8")),
    )
    return HTMLResponse(content=body_html)


__all__ = ["router"]
