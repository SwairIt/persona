"""Manage process-deny / allow-only lists for the capture loop."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.capture import (
    default_deny_list,
    load_user_lists,
    save_user_lists,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["whitelist"])


@router.get("/whitelist", response_class=HTMLResponse)
async def whitelist_page(request: Request) -> HTMLResponse:
    user = load_user_lists()
    return templates.TemplateResponse(
        request,
        "whitelist.html",
        {
            "title": "Process whitelist",
            "active_nav": "settings",
            "defaults": default_deny_list(),
            "user_deny": user["deny"],
            "user_allow_only": user["allow_only"],
        },
    )


@router.post("/whitelist", response_class=HTMLResponse)
async def whitelist_save(
    request: Request,
    deny: str = Form(default=""),
    allow_only: str = Form(default=""),
) -> RedirectResponse:
    deny_list = _split(deny)
    allow_list = _split(allow_only)
    save_user_lists(deny_list, allow_list)
    return RedirectResponse(url="/whitelist", status_code=303)


def _split(value: str) -> list[str]:
    return [p.strip().lower() for p in value.replace(",", "\n").splitlines() if p.strip()]
