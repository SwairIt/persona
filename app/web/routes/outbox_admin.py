"""Admin UI for outbox webhook templates (v1.28).

CRUD on ``outbox_template`` — pre-baked Linear / Notion / Slack /
generic request shapes the dispatcher renders with each event payload
and POSTs to the upstream service. See :mod:`app.outbox` for the
runtime contract and the documented ``event_kind`` vocabulary.

Routes:

    GET  /settings/outbox                — list templates + add form
    POST /settings/outbox/new            — insert a new template
    POST /settings/outbox/{id}/delete    — drop a template
    POST /settings/outbox/{id}/toggle    — flip ``enabled``
    GET  /api/outbox/templates.json      — JSON dump for tooling

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.outbox import _KNOWN_EVENT_KINDS
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.outbox.admin")

router = APIRouter(tags=["outbox"])

_ALLOWED_SERVICES: frozenset[str] = frozenset({"linear", "notion", "slack", "generic"})
"""Mirror of the migration's CHECK constraint. Validated in Python so the
admin form yields a 400 instead of a SQLite IntegrityError on a typo."""


async def _list_templates(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return every row in ``outbox_template``, including disabled ones."""
    cursor = await conn.execute(
        "SELECT id, name, service, event_kind, target_url, auth_header, "
        "body_template, enabled, created_at "
        "FROM outbox_template "
        "ORDER BY id DESC",
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "service": str(row["service"]),
            "event_kind": str(row["event_kind"]),
            "target_url": str(row["target_url"]),
            "auth_header": (
                str(row["auth_header"]) if row["auth_header"] is not None else None
            ),
            "body_template": str(row["body_template"]),
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


@router.get("/settings/outbox", response_class=HTMLResponse)
async def outbox_page(request: Request) -> HTMLResponse:
    """Render the outbox template management page."""
    async with get_connection() as conn:
        rows = await _list_templates(conn)
    return templates.TemplateResponse(
        request,
        "outbox_admin.html",
        {
            "title": "Webhook outbox",
            "active_nav": "settings",
            "templates_rows": rows,
            "services": sorted(_ALLOWED_SERVICES),
            "event_kinds": sorted(_KNOWN_EVENT_KINDS),
        },
    )


@router.post("/settings/outbox/new")
async def outbox_create(
    name: str = Form(...),
    service: str = Form(...),
    event_kind: str = Form(...),
    target_url: str = Form(...),
    auth_header: str = Form(""),
    body_template: str = Form(...),
) -> RedirectResponse:
    """Insert a new template, then 303-redirect back to the list."""
    name_clean = name.strip()
    service_clean = service.strip().lower()
    event_kind_clean = event_kind.strip()
    target_url_clean = target_url.strip()
    body_template_clean = body_template.strip()
    auth_value: str | None = auth_header.strip() or None

    if not name_clean:
        raise HTTPException(status_code=400, detail="name is required")
    if service_clean not in _ALLOWED_SERVICES:
        raise HTTPException(
            status_code=400,
            detail=f"service must be one of: {', '.join(sorted(_ALLOWED_SERVICES))}",
        )
    if not event_kind_clean:
        raise HTTPException(status_code=400, detail="event_kind is required")
    if not target_url_clean:
        raise HTTPException(status_code=400, detail="target_url is required")
    if not body_template_clean:
        raise HTTPException(status_code=400, detail="body_template is required")

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO outbox_template "
            "(name, service, event_kind, target_url, auth_header, body_template) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name_clean,
                service_clean,
                event_kind_clean,
                target_url_clean,
                auth_value,
                body_template_clean,
            ),
        )
        await conn.commit()
    log.info(
        "outbox.template_created",
        name=name_clean,
        service=service_clean,
        event_kind=event_kind_clean,
        has_auth=auth_value is not None,
    )
    return RedirectResponse(url="/settings/outbox", status_code=303)


@router.post("/settings/outbox/{template_id}/delete")
async def outbox_delete(template_id: int) -> RedirectResponse:
    """Drop a template, then 303-redirect back to the list."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM outbox_template WHERE id = ?",
            (int(template_id),),
        )
        await conn.commit()
    log.info("outbox.template_deleted", template_id=int(template_id))
    return RedirectResponse(url="/settings/outbox", status_code=303)


@router.post("/settings/outbox/{template_id}/toggle")
async def outbox_toggle(template_id: int) -> RedirectResponse:
    """Flip ``enabled`` for one template, then 303-redirect back to the list."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE outbox_template SET enabled = 1 - enabled WHERE id = ?",
            (int(template_id),),
        )
        await conn.commit()
    log.info("outbox.template_toggled", template_id=int(template_id))
    return RedirectResponse(url="/settings/outbox", status_code=303)


@router.get("/api/outbox/templates.json")
async def outbox_templates_json() -> JSONResponse:
    """Return every template as JSON. Includes disabled rows."""
    async with get_connection() as conn:
        rows = await _list_templates(conn)
    return JSONResponse(rows)


__all__ = ["router"]
