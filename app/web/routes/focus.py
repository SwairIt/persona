"""Focus-mode (Pomodoro) endpoint + UI."""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.storage.db import get_connection
from app.storage.focus import (
    finish_session,
    list_recent_sessions,
    session_count_today,
    start_session,
)
from app.web.templates_engine import templates
from app.workers.control import get_controller

router = APIRouter(tags=["focus"])


@router.get("/focus", response_class=HTMLResponse)
async def focus_page(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        recent = await list_recent_sessions(conn, limit=10)
        completed_today = await session_count_today(conn, date.today().isoformat())
    return templates.TemplateResponse(
        request,
        "focus.html",
        {
            "title": "Focus mode",
            "active_nav": "focus",
            "recent": recent,
            "completed_today": completed_today,
        },
    )


@router.post("/api/focus/start", response_class=JSONResponse)
async def focus_start(
    duration_minutes: int = Form(default=25, ge=1, le=240),
    intent: str = Form(default=""),
    pause_capture: bool = Form(default=True),
) -> JSONResponse:
    async with get_connection() as conn:
        session_id = await start_session(
            conn,
            started_at=datetime.now(timezone.utc),
            duration_minutes=duration_minutes,
            intent=intent.strip() or None,
        )
    if pause_capture:
        get_controller().pause()
    return JSONResponse(
        {
            "session_id": session_id,
            "duration_minutes": duration_minutes,
            "capture_paused": pause_capture,
        }
    )


@router.post("/api/focus/finish", response_class=JSONResponse)
async def focus_finish(
    session_id: int = Form(...),
    completed: bool = Form(default=True),
    outcome: str = Form(default=""),
    resume_capture: bool = Form(default=True),
) -> JSONResponse:
    async with get_connection() as conn:
        await finish_session(
            conn,
            session_id,
            ended_at=datetime.now(timezone.utc),
            completed=completed,
            outcome=outcome.strip() or None,
        )
    if resume_capture:
        get_controller().resume()
    return JSONResponse({"session_id": session_id, "completed": completed})


@router.get("/api/focus/sessions", response_class=JSONResponse)
async def focus_sessions(limit: int = 30) -> JSONResponse:
    async with get_connection() as conn:
        sessions = await list_recent_sessions(conn, limit=limit)
    serialisable = [
        {
            "id": s["id"],
            "started_at": s["started_at"].isoformat(),
            "ended_at": s["ended_at"].isoformat() if s["ended_at"] else None,
            "duration_minutes": s["duration_minutes"],
            "intent": s["intent"],
            "outcome": s["outcome"],
            "completed": s["completed"],
        }
        for s in sessions
    ]
    return JSONResponse({"sessions": serialisable})
