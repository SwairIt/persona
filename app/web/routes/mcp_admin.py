"""T24 — /admin/mcp page for managing MCP server configs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.mcp import (
    delete_server,
    list_servers,
    set_command,
    set_enabled,
    upsert_server,
)
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.mcp.admin")

router = APIRouter(tags=["mcp"])


async def _load_timeouts() -> dict[int, int]:
    """{server_id: timeout_ms} — пер-серверный таймаут MCP-вызова (миграция 202).

    Best-effort: колонка timeout_ms могла не примениться на старой БД — тогда
    тихо возвращаем пустую карту (UI покажет пустое поле = «дефолт»).
    """
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT id, timeout_ms FROM mcp_server WHERE timeout_ms IS NOT NULL"
            )
            rows = await cur.fetchall()
        out: dict[int, int] = {}
        for r in rows:
            try:
                out[int(r["id"])] = int(r["timeout_ms"])
            except (TypeError, ValueError):
                continue
        return out
    except Exception as exc:  # noqa: BLE001 — нет колонки → пусто
        log.debug("mcp.admin.timeouts_unavailable", error=str(exc))
        return {}


async def _set_timeout(server_id: int, timeout_ms: int | None) -> None:
    """Сохранить пер-серверный timeout_ms (None/<=0 → NULL = дефолт). Best-effort."""
    value = timeout_ms if (timeout_ms is not None and timeout_ms > 0) else None
    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE mcp_server SET timeout_ms = ? WHERE id = ?",
                (value, server_id),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001 — нет колонки → no-op (не ломаем edit)
        log.debug("mcp.admin.set_timeout_failed", error=str(exc))


@router.get("/admin/mcp", response_class=HTMLResponse, response_model=None)
async def mcp_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    servers = await list_servers()
    # Подмешиваем пер-серверный timeout_ms (миграция 202) для UI.
    timeouts = await _load_timeouts()
    for s in servers:
        s["timeout_ms"] = timeouts.get(int(s["id"]))
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
    timeout_ms: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if server_id <= 0 or not command.strip():
        raise HTTPException(status_code=400)
    await set_command(server_id, command.strip())
    # Пер-серверный таймаут (мс). Пусто/0/нечисло → NULL = дефолтный _RPC_TIMEOUT.
    raw = (timeout_ms or "").strip()
    parsed: int | None = None
    if raw:
        try:
            parsed = int(float(raw))
        except (TypeError, ValueError):
            parsed = None
    await _set_timeout(server_id, parsed)
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
