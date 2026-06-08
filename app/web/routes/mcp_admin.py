"""T24 — /admin/mcp page for managing MCP server configs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.mcp import (
    delete_server,
    list_servers,
    set_command,
    set_enabled,
    upsert_server,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["mcp"])


@router.get("/admin/mcp", response_class=HTMLResponse, response_model=None)
async def mcp_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    servers = await list_servers()
    return templates.TemplateResponse(
        request,
        "mcp_admin.html",
        {
            "title": "MCP серверы",
            "active_nav": "",
            "servers": servers,
        },
    )


@router.post("/admin/mcp/toggle", response_model=None)
async def mcp_toggle(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    server_id: Annotated[int, Form()] = 0,
    enabled: Annotated[str, Form()] = "0",
) -> RedirectResponse:
    if server_id <= 0:
        raise HTTPException(status_code=400)
    await set_enabled(server_id, enabled in ("1", "true", "on"))
    return RedirectResponse(url="/admin/mcp", status_code=303)


@router.post("/admin/mcp/edit", response_model=None)
async def mcp_edit(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    server_id: Annotated[int, Form()] = 0,
    command: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if server_id <= 0 or not command.strip():
        raise HTTPException(status_code=400)
    await set_command(server_id, command.strip())
    return RedirectResponse(url="/admin/mcp", status_code=303)


@router.post("/admin/mcp/add", response_model=None)
async def mcp_add(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    command: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if not name.strip() or not command.strip():
        raise HTTPException(status_code=400)
    await upsert_server(
        name=name.strip(),
        description=description.strip() or None,
        command=command.strip(),
        enabled=False,
    )
    return RedirectResponse(url="/admin/mcp", status_code=303)


@router.post("/admin/mcp/delete", response_model=None)
async def mcp_delete(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    server_id: Annotated[int, Form()] = 0,
) -> RedirectResponse:
    if server_id > 0:
        await delete_server(server_id)
    return RedirectResponse(url="/admin/mcp", status_code=303)
