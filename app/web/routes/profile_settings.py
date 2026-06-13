"""T29 — "About me" profile editor. The text is injected into every chat so
the AI knows who the user is."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.profile import get_profile, set_profile
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.profile.settings")


async def _render(request: Request, user_id: int, *, saved: bool) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "profile_settings.html",
        {
            "title": "Профиль — обо мне",
            "active_nav": "settings",
            "profile": await get_profile(user_id),
            "saved": saved,
        },
    )


@router.get("/settings/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return await _render(request, session["user_id"], saved=False)


@router.post("/settings/profile", response_class=HTMLResponse, response_model=None)
async def profile_save(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    profile_text: str = Form(default=""),
) -> HTMLResponse:
    await set_profile(session["user_id"], profile_text)
    log.info("profile.saved", user_id=session["user_id"], length=len(profile_text or ""))
    return await _render(request, session["user_id"], saved=True)
