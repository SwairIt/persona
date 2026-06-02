"""Liveness probe and welcome / first-run wizard."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import __version__
from app.ocr import probe_tesseract
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.time import iso
from app.web.templates_engine import templates
from app.workers.control import get_controller

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> JSONResponse:
    controller = get_controller()
    settings = get_settings()
    db_ok = True
    db_error = None
    try:
        async with get_connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        db_ok = False
        db_error = str(exc)[:200]

    payload = {
        "version": __version__,
        "now": iso(datetime.now(timezone.utc)),
        "db_ok": db_ok,
        "db_error": db_error,
        "paused": controller.paused,
        "captures_total": controller.captures_total,
        "host": settings.host,
        "port": settings.port,
        "ocr_enabled": settings.ocr_enabled,
        "tesseract_available": probe_tesseract(settings.tesseract_path).available,
    }
    status_code = 200 if db_ok else 503
    return JSONResponse(payload, status_code=status_code)


@router.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request) -> HTMLResponse:
    settings = get_settings()
    probe = probe_tesseract(settings.tesseract_path)
    return templates.TemplateResponse(
        request,
        "welcome.html",
        {
            "title": "Welcome",
            "active_nav": "",
            "settings": settings,
            "tesseract": probe,
        },
    )
