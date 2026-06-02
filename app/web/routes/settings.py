"""Settings page — display config + kv overrides + Tesseract probe."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.ocr import probe_tesseract
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import list_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render the settings page."""
    cfg = get_settings()
    probe = probe_tesseract(cfg.tesseract_path)

    async with get_connection() as conn:
        overrides = await list_kv(conn)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "title": "Settings",
            "active_nav": "settings",
            "settings": cfg,
            "overrides": overrides,
            "tesseract": probe,
        },
    )


@router.post("/settings/override", response_class=HTMLResponse)
async def update_override(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
) -> RedirectResponse:
    """Upsert a kv override. Currently informational — no live reload of Settings."""
    async with get_connection() as conn:
        await set_kv(conn, key, value)
    return RedirectResponse(url="/settings", status_code=303)
