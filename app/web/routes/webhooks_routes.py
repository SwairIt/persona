"""Webhooks management UI + JSON CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.webhooks import (
    create_webhook,
    delete_webhook,
    list_webhooks,
    toggle_webhook,
)
from app.web.templates_engine import templates
from app.webhooks.dispatcher import VALID_EVENTS, dispatch_test

router = APIRouter(tags=["webhooks"])


@router.get("/webhooks", response_class=HTMLResponse)
async def webhooks_page(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        subs = await list_webhooks(conn)
    return templates.TemplateResponse(
        request,
        "webhooks.html",
        {
            "title": "Webhooks",
            "active_nav": "settings",
            "subs": subs,
            "events": sorted(VALID_EVENTS),
        },
    )


@router.post("/webhooks", response_class=HTMLResponse)
async def webhooks_create(
    request: Request,
    url: str = Form(...),
    event_type: str = Form(...),
    secret: str = Form(default=""),
) -> RedirectResponse:
    if event_type not in VALID_EVENTS:
        raise HTTPException(status_code=400, detail=f"Unknown event: {event_type}")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must be http:// or https://")
    async with get_connection() as conn:
        await create_webhook(conn, url=url, event_type=event_type, secret=secret or None)
    return RedirectResponse(url="/webhooks", status_code=303)


@router.post("/webhooks/{webhook_id}/toggle")
async def webhooks_toggle(webhook_id: int, enabled: bool = Form(...)) -> RedirectResponse:
    async with get_connection() as conn:
        await toggle_webhook(conn, webhook_id, enabled)
    return RedirectResponse(url="/webhooks", status_code=303)


@router.post("/webhooks/{webhook_id}/delete")
async def webhooks_delete(webhook_id: int) -> RedirectResponse:
    async with get_connection() as conn:
        await delete_webhook(conn, webhook_id)
    return RedirectResponse(url="/webhooks", status_code=303)


@router.post("/api/webhooks/{webhook_id}/test")
async def webhooks_test(webhook_id: int) -> JSONResponse:
    """Fire a synthetic *signed* event at the webhook so the user can verify delivery.

    Delegates to :func:`app.webhooks.dispatcher.dispatch_test` so the
    receiver sees the exact same signed envelope (X-Persona-Signature +
    X-Persona-Timestamp headers) as a real production event.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM webhooks WHERE id = ?",
            (webhook_id,),
        )
        exists = await cursor.fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="webhook not found")

    result = await dispatch_test(webhook_id)
    if not result.get("ok"):
        return JSONResponse(
            {"webhook_id": webhook_id, "queued": False, "reason": result.get("reason")},
            status_code=409,
        )
    return JSONResponse(
        {"webhook_id": webhook_id, "event_type": result["event_type"], "queued": True}
    )
