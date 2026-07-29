"""Owner-only controls and history for Persona's adaptive prompt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.owner import is_primary_owner
from app.chat.dynamic_prompt import (
    activate_version,
    get_config,
    list_versions,
    set_enabled,
)
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from app.auth.sessions import SessionRecord

router = APIRouter(tags=["settings"])

_MODE_LABELS = {
    "social": "живой",
    "casual": "неформальный",
    "playful": "игривый",
    "supportive": "поддерживающий",
    "creative": "творческий",
    "focused": "сфокусированный",
    "serious": "серьёзный",
}


async def _primary_owner_id(session: SessionRecord) -> int:
    user_id = int(session["user_id"])
    if not await is_primary_owner(user_id):
        raise HTTPException(status_code=403, detail="Только основной владелец")
    return user_id


@router.get("/settings/system-prompt/history", response_class=HTMLResponse)
async def dynamic_prompt_history_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    user_id = await _primary_owner_id(session)
    enabled, learned_rules = await get_config(user_id)
    versions = await list_versions(user_id, limit=200)
    return templates.TemplateResponse(
        request,
        "dynamic_prompt_history.html",
        {
            "title": "Живой характер Persona",
            "active_nav": "settings",
            "enabled": enabled,
            "learned_rules": learned_rules,
            "versions": versions,
            "mode_labels": _MODE_LABELS,
        },
    )


@router.post("/settings/system-prompt/history/toggle", response_model=None)
async def dynamic_prompt_toggle(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    enabled: str = Form(default="0"),
) -> RedirectResponse:
    user_id = await _primary_owner_id(session)
    await set_enabled(user_id, enabled == "1")
    return RedirectResponse(url="/settings/system-prompt/history", status_code=303)


@router.post(
    "/settings/system-prompt/history/{version_id}/activate",
    response_model=None,
)
async def dynamic_prompt_activate(
    version_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    user_id = await _primary_owner_id(session)
    if not await activate_version(user_id, version_id):
        raise HTTPException(status_code=404, detail="Версия не найдена")
    return RedirectResponse(url="/settings/system-prompt/history", status_code=303)


__all__ = ["router"]
