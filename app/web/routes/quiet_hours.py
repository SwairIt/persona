"""Quiet-hours admin — recurring weekly windows that auto-pause capture."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.quiet_hours import (
    create_rule,
    delete_rule,
    is_quiet_now,
    list_rules,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["quiet-hours"])

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@router.get("/quiet-hours", response_class=HTMLResponse)
async def quiet_hours_page(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        rules = await list_rules(conn)
        quiet_now = await is_quiet_now(conn)
    return templates.TemplateResponse(
        request,
        "quiet_hours.html",
        {
            "title": "Quiet hours",
            "active_nav": "settings",
            "rules": rules,
            "quiet_now": quiet_now,
            "weekday_names": _WEEKDAY_NAMES,
            "hours": list(range(24)),
            "end_hours": list(range(1, 25)),
        },
    )


@router.post("/quiet-hours")
async def quiet_hours_create(
    weekday: int = Form(...),
    start_hour: int = Form(...),
    end_hour: int = Form(...),
    label: str = Form(default=""),
) -> RedirectResponse:
    try:
        async with get_connection() as conn:
            await create_rule(
                conn,
                weekday=weekday,
                start_hour=start_hour,
                end_hour=end_hour,
                label=label or None,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/quiet-hours", status_code=303)


@router.post("/quiet-hours/{rule_id}/delete")
async def quiet_hours_delete(rule_id: int) -> RedirectResponse:
    async with get_connection() as conn:
        await delete_rule(conn, rule_id)
    return RedirectResponse(url="/quiet-hours", status_code=303)
