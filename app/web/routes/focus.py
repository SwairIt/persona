"""Focus-mode (Pomodoro) endpoint + UI — v0.36.

Routes:
    * ``GET  /focus``                 — HTML page with countdown timer +
                                        recent sessions list.
    * ``POST /focus/start``           — create a new session
                                        (``work_minutes`` / ``break_minutes`` /
                                        ``label``); redirects back to
                                        ``/focus`` so the page reloads with
                                        the newly-active timer.
    * ``POST /focus/end``             — close the current session
                                        (``session_id`` + ``completed``);
                                        redirects back to ``/focus``.
    * ``GET  /api/focus/current.json``— JSON snapshot of the open session
                                        (or ``{"session": null}`` if none),
                                        consumed by the client clock.

The page renders the countdown via inline JavaScript driven by the
client clock — the server only hands over ``started_at`` and the work
window, and the browser computes the remaining seconds. When the timer
hits zero we beep via the Web Audio API (no library, ~25 lines) so the
notification works even when the tab is in the background but still
audible.

Optional capture-pause: if ``pause_capture=1`` is posted with ``/focus/start``
the global :class:`~app.workers.control.CaptureController` is paused for
the duration of the break — same controller the header status badge in
``base.html`` polls, so the "Paused" pill lights up immediately.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.focus import (
    current_session,
    end_session,
    recent_sessions,
    start_session,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates
from app.workers.control import get_controller

log = get_logger("persona.focus")

router = APIRouter(tags=["focus"])


@router.get("/focus", response_class=HTMLResponse)
async def focus_page(request: Request) -> HTMLResponse:
    """Render the Pomodoro page — big countdown + recent sessions."""
    active = await current_session()
    recent = await recent_sessions(days=7)
    log.info(
        "focus.page",
        has_active=active is not None,
        recent_count=len(recent),
    )
    return templates.TemplateResponse(
        request,
        "focus.html",
        {
            "title": "Focus",
            "active_nav": "focus",
            "active": active,
            "recent": recent,
        },
    )


@router.post("/focus/start")
async def focus_start(
    work_minutes: int = Form(default=25, ge=1, le=240),
    break_minutes: int = Form(default=5, ge=0, le=60),
    label: str = Form(default=""),
    pause_capture: int = Form(default=0),
) -> RedirectResponse:
    """Create a new focus session and (optionally) pause screen capture."""
    session_id = await start_session(
        work_minutes=work_minutes,
        break_minutes=break_minutes,
        label=label.strip() or None,
    )
    if pause_capture == 1:
        get_controller().pause()
    log.info(
        "focus.started",
        session_id=session_id,
        capture_paused=pause_capture == 1,
    )
    return RedirectResponse(url="/focus", status_code=303)


@router.post("/focus/end")
async def focus_end(
    session_id: int = Form(...),
    completed: int = Form(default=0),
    resume_capture: int = Form(default=1),
) -> RedirectResponse:
    """Close the given session and (optionally) resume screen capture."""
    await end_session(session_id=session_id, completed=bool(completed))
    if resume_capture == 1:
        get_controller().resume()
    log.info(
        "focus.ended",
        session_id=session_id,
        completed=bool(completed),
        capture_resumed=resume_capture == 1,
    )
    return RedirectResponse(url="/focus", status_code=303)


@router.get("/api/focus/current.json", response_class=JSONResponse)
async def focus_current_json() -> JSONResponse:
    """JSON snapshot of the currently-open session (or ``null``)."""
    active = await current_session()
    if active is None:
        return JSONResponse({"session": None})
    return JSONResponse({"session": dict(active)})
