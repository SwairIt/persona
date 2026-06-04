"""Admin UI for the regex-based capture blocklist (v1.21).

Lets the operator manage the ``capture_regex_blocklist`` table —
patterns matched against the foreground window's ``app_name`` and/or
``window_title``. Stricter sibling of
:mod:`app.web.routes.app_capture_skip`, which only handles exact app
names.

Routes:

    GET  /settings/blocklist                — list rules + add form
    POST /settings/blocklist                — insert a new rule
    POST /settings/blocklist/{id}/delete    — drop a rule
    POST /settings/blocklist/{id}/toggle    — flip ``enabled``
    GET  /api/blocklist.json                — JSON dump for tooling

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.capture_blocklist import (
    add_rule,
    delete_rule,
    list_rules,
    toggle_rule,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.capture_blocklist.admin")

router = APIRouter(tags=["capture-blocklist"])


@router.get("/settings/blocklist", response_class=HTMLResponse)
async def capture_blocklist_page(request: Request) -> HTMLResponse:
    """Render the regex-blocklist management page."""
    async with get_connection() as conn:
        rows = await list_rules(conn)
    return templates.TemplateResponse(
        request,
        "capture_blocklist.html",
        {
            "title": "Регекс-блоклист",
            "active_nav": "settings",
            "rules": rows,
        },
    )


@router.post("/settings/blocklist")
async def capture_blocklist_create(
    pattern: str = Form(...),
    field: str = Form(...),
    description: str = Form(""),
) -> RedirectResponse:
    """Insert a new rule, then 303-redirect back to the list."""
    try:
        async with get_connection() as conn:
            await add_rule(
                conn,
                pattern=pattern,
                field=field,
                description=description,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/blocklist", status_code=303)


@router.post("/settings/blocklist/{rule_id}/delete")
async def capture_blocklist_delete(rule_id: int) -> RedirectResponse:
    """Drop a rule by id, then 303-redirect back to the list."""
    async with get_connection() as conn:
        await delete_rule(conn, rule_id)
    return RedirectResponse(url="/settings/blocklist", status_code=303)


@router.post("/settings/blocklist/{rule_id}/toggle")
async def capture_blocklist_toggle(rule_id: int) -> RedirectResponse:
    """Flip the ``enabled`` flag, then 303-redirect back to the list."""
    async with get_connection() as conn:
        await toggle_rule(conn, rule_id)
    return RedirectResponse(url="/settings/blocklist", status_code=303)


@router.get("/api/blocklist.json")
async def capture_blocklist_json() -> JSONResponse:
    """Return every rule as JSON. Includes disabled rows."""
    async with get_connection() as conn:
        rows = await list_rules(conn)
    payload: list[dict[str, Any]] = [
        {
            "id": int(row["id"]),
            "pattern": str(row["pattern"]),
            "field": str(row["field"]),
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"]),
            "description": (
                str(row["description"]) if row["description"] is not None else None
            ),
        }
        for row in rows
    ]
    return JSONResponse(payload)


__all__ = ["router"]
