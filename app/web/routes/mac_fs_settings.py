"""T29 — settings for AI-on-Mac filesystem access (allowlist + toggle)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.devices.fs_rpc import get_roots, is_enabled, set_enabled, set_roots
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.mac_fs.settings")


async def _render(request: Request, *, saved: bool) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "mac_fs_settings.html",
        {
            "title": "AI на Mac (файлы)",
            "active_nav": "settings",
            "enabled": await is_enabled(),
            "roots": await get_roots(),
            "saved": saved,
        },
    )


@router.get("/settings/mac-fs", response_class=HTMLResponse)
async def mac_fs_page(request: Request) -> HTMLResponse:
    return await _render(request, saved=False)


@router.post("/settings/mac-fs", response_class=HTMLResponse, response_model=None)
async def mac_fs_save(
    request: Request,
    enabled: str = Form(default=""),
    roots: str = Form(default=""),
) -> HTMLResponse:
    await set_enabled(enabled in ("on", "1", "true"))
    await set_roots([r for r in roots.splitlines()])
    log.info("mac_fs.saved", enabled=enabled)
    return await _render(request, saved=True)
