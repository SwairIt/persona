"""Static help page with keyboard shortcuts and quick tips."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.templates_engine import templates

router = APIRouter(tags=["help"])


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "help.html",
        {"title": "Help", "active_nav": "help"},
    )
