"""Today-only reminders — short todo list shown in side bar."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.reminders import (
    create_reminder,
    delete_reminder,
    list_for_day,
    list_pending_anywhere,
    toggle_done,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["reminders"])


@router.get("/reminders", response_class=HTMLResponse)
async def reminders_page(request: Request, day: str | None = None) -> HTMLResponse:
    target = _parse_day(day)
    async with get_connection() as conn:
        for_today = await list_for_day(conn, day=target)
        overdue = [r for r in await list_pending_anywhere(conn) if r["due_date"] != target.isoformat()]
    return templates.TemplateResponse(
        request,
        "reminders.html",
        {
            "title": "Reminders",
            "active_nav": "reminders",
            "day": target,
            "items": for_today,
            "overdue": overdue,
        },
    )


@router.post("/reminders/create")
async def reminders_create(
    body: str = Form(...),
    due_date: str = Form(...),
    screenshot_id: str | None = Form(None),
) -> RedirectResponse:
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")
    try:
        d = datetime.strptime(due_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid due_date") from exc
    shot_id: int | None = None
    if screenshot_id is not None and screenshot_id != "":
        try:
            shot_id = int(screenshot_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid screenshot_id") from exc
    async with get_connection() as conn:
        await create_reminder(conn, body=body, due_date=d, screenshot_id=shot_id)
    return RedirectResponse(url=f"/reminders?day={d.isoformat()}", status_code=303)


@router.post("/reminders/{reminder_id}/toggle")
async def reminders_toggle(reminder_id: int, done: bool = Form(...)) -> RedirectResponse:
    async with get_connection() as conn:
        await toggle_done(conn, reminder_id, done)
    return RedirectResponse(url="/reminders", status_code=303)


@router.post("/reminders/{reminder_id}/delete")
async def reminders_delete(reminder_id: int) -> RedirectResponse:
    async with get_connection() as conn:
        await delete_reminder(conn, reminder_id)
    return RedirectResponse(url="/reminders", status_code=303)


@router.post("/api/screenshots/{screenshot_id}/remind")
async def api_screenshot_remind(
    screenshot_id: int,
    body: str = Form(...),
    due_date: str = Form(...),
) -> JSONResponse:
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")
    try:
        d = datetime.strptime(due_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid due_date") from exc
    async with get_connection() as conn:
        reminder_id = await create_reminder(
            conn, body=body, due_date=d, screenshot_id=screenshot_id
        )
    return JSONResponse({"reminder_id": reminder_id, "due_date": d.isoformat()})


def _parse_day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return date.today()
