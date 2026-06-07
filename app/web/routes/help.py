"""Static help page with keyboard shortcuts and quick tips.

T10 (2026-06-07): the original keyboard-shortcuts page moved to a
secondary URL so the friendly walkthrough at /help (in
``help_walkthrough.py``) is the page users actually land on. The two
routes were both registered as ``/help`` and FastAPI silently shadowed
the newer Russian walkthrough with this older English page — confusing
users and the maintainer.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.templates_engine import templates

router = APIRouter(tags=["help"])


@router.get("/help/shortcuts", response_class=HTMLResponse)
async def help_shortcuts(request: Request) -> HTMLResponse:
    """Legacy keyboard-shortcuts cheatsheet. Linked from /help."""
    return templates.TemplateResponse(
        request,
        "help.html",
        {"title": "Горячие клавиши", "active_nav": "help"},
    )
