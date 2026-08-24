"""Онбординг участника — понятный пайплайн до рабочего ИИ-ассистента.

Не-владелец после первого входа попадает сюда (см. _post_auth_dest): короткие шаги
«что у тебя есть и что делать», затем кнопка → /chat. Флаг onboarded_<uid> в kv,
чтобы потом сразу открывался чат.

MVP «бесплатно со своим ключом»: биллинга/триала здесь нет вообще, главный шаг —
подключить СВОЮ LLM в /settings/llm (гайд — /help/connect-llm). Страница знает
только один факт про модель: ``llm_ready`` (есть рабочая конфигурация или нет),
никаких ключей в шаблон не утекает.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import __version__
from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.llm.client import user_llm_configured
from app.storage.db import get_connection
from app.storage.repository import set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["onboarding"])


@router.get("/onboarding", response_class=HTMLResponse, response_model=None)
async def onboarding_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> Response:
    uid = int(session["user_id"])
    if await is_owner(uid):
        return RedirectResponse(url="/now", status_code=303)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "title": "Добро пожаловать",
            "active_nav": "",
            "is_owner": False,
            "app_version": __version__,
            "llm_ready": await user_llm_configured(uid),
            "email": session.get("email"),
        },
    )


@router.post("/onboarding/complete", response_model=None)
async def onboarding_complete(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    uid = int(session["user_id"])
    async with get_connection() as conn:
        await set_kv(conn, f"onboarded_{uid}", "1")
    return RedirectResponse(url="/chat", status_code=303)
