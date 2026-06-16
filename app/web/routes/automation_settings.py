"""Phase 2 — /settings/automation: browser backend + MCP runtime switch.

One settings page controlling the two new automation subsystems:

* **browser_backend** (kv ``browser_backend``) — ``builtin`` | ``mcp`` |
  ``both``. Chooses whether the chat's interactive browser tools are served
  by Persona's own per-session Playwright worker
  (:mod:`app.browse.agent`), an external Playwright MCP server, or both.
* **mcp_runtime_enabled** (kv ``mcp_runtime_enabled``) — master switch for
  the stdio MCP runtime (:mod:`app.mcp.runtime`). When on, enabled,
  allowlisted, non-builtin ``mcp_server`` rows are launched and their tools
  discovered as ``mcp__server__tool``.
* **browser_allow_domains / browser_deny_domains** (kv) — optional newline /
  comma separated domain lists. Allow empty = allow-all (localhost / private
  ranges are ALWAYS blocked regardless). Deny wins over allow.

This route does NOT register itself with the FastAPI app — wire it up in
``app.web.main`` alongside the other routers::

    from app.web.routes import automation_settings
    app.include_router(automation_settings.router)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings", "automation"])

log = get_logger("persona.automation.settings")

_VALID_BACKENDS = ("builtin", "mcp", "both")


async def _read_state() -> dict[str, str]:
    async with get_connection() as conn:
        backend = (await get_kv(conn, "browser_backend") or "builtin").strip().lower()
        mcp_on = (await get_kv(conn, "mcp_runtime_enabled") or "0").strip()
        allow = await get_kv(conn, "browser_allow_domains") or ""
        deny = await get_kv(conn, "browser_deny_domains") or ""
    if backend not in _VALID_BACKENDS:
        backend = "builtin"
    return {
        "browser_backend": backend,
        "mcp_runtime_enabled": "1" if mcp_on == "1" else "0",
        "browser_allow_domains": allow,
        "browser_deny_domains": deny,
    }


@router.get("/settings/automation", response_class=HTMLResponse)
async def automation_settings_page(request: Request) -> HTMLResponse:
    """Render the automation settings page."""
    state = await _read_state()
    # Show what the MCP runtime currently discovered (best-effort, for trust).
    discovered: list[str] = []
    try:
        from app.mcp.runtime import discovered_mcp_tools  # noqa: PLC0415

        discovered = await discovered_mcp_tools()
    except Exception as exc:  # noqa: BLE001
        log.debug("automation.discover_failed", error=str(exc))
    return templates.TemplateResponse(
        request,
        "automation_settings.html",
        {
            "title": "Автоматизация — браузер и MCP",
            "active_nav": "settings",
            "state": state,
            "backends": _VALID_BACKENDS,
            "discovered": discovered,
        },
    )


@router.post("/settings/automation", response_class=JSONResponse)
async def automation_settings_save(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    request: Request,
) -> JSONResponse:
    """Persist the automation settings. JSON body, all keys optional."""
    _ = session
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    updates: dict[str, str] = {}
    if "browser_backend" in body:
        backend = str(body.get("browser_backend", "")).strip().lower()
        if backend not in _VALID_BACKENDS:
            return JSONResponse({"ok": False, "error": "bad browser_backend"}, status_code=400)
        updates["browser_backend"] = backend
    if "mcp_runtime_enabled" in body:
        on = body.get("mcp_runtime_enabled")
        truthy = on is True or str(on).strip().lower() in ("1", "true", "on")
        updates["mcp_runtime_enabled"] = "1" if truthy else "0"
    if "browser_allow_domains" in body:
        updates["browser_allow_domains"] = str(body.get("browser_allow_domains") or "").strip()
    if "browser_deny_domains" in body:
        updates["browser_deny_domains"] = str(body.get("browser_deny_domains") or "").strip()

    async with get_connection() as conn:
        for key, value in updates.items():
            await set_kv(conn, key, value)

    # If the MCP runtime was just turned OFF, tear running servers down so the
    # change takes effect immediately (no stale subprocesses).
    if updates.get("mcp_runtime_enabled") == "0":
        try:
            from app.mcp.runtime import shutdown_mcp_runtime  # noqa: PLC0415

            await shutdown_mcp_runtime()
        except Exception as exc:  # noqa: BLE001
            log.debug("automation.mcp_shutdown_failed", error=str(exc))

    log.info("automation.settings_saved", keys=list(updates.keys()))
    return JSONResponse({"ok": True, "state": await _read_state()})


__all__ = ["router"]
