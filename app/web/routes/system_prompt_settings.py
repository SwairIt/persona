"""T29 — chat system-prompt picker/editor.

Lets the user choose a base system prompt for chat (default + curated
presets adapted from Anthropic's Claude Code prompts), edit the full text
freely, save it, or reset to default. A per-session "роль" still overrides
the saved prompt.

Кто чей промпт видит (2026-08)
------------------------------
Раньше у роутера НЕ БЫЛО зависимости аутентификации, а kv-строка
``chat_system_prompt`` была ОДНА на инстанс: любой зарегистрированный
участник открывал эту страницу и читал ЛИЧНЫЙ характер владельца (часто с
именами и деталями его жизни), а сохранением — перезаписывал его всем.
Теперь роутер требует сессию, и личность решает источник:

* владелец → глобальный ``kv_settings`` (поведение 1:1 прежнее);
* участник → его строка в ``user_settings`` с тем же ключом; своей строки
  нет → встроенный ``DEFAULT_SYSTEM_PROMPT``, НИКОГДА не текст владельца.

Пресеты работают у обоих: чипы — чистый клиентский JS, submit идёт в тот
же POST, который уже пишет per-user.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.chat import (
    DEFAULT_SYSTEM_PROMPT,
    PRESETS,
    get_active_system_prompt,
    is_custom_system_prompt,
    reset_active_system_prompt,
    set_active_system_prompt,
)
from app.logging_setup import get_logger

# Fail-closed резолв роли: сбой резолва = «участник» (app/web/routes/owner_view.py).
from app.web.routes.owner_view import viewer_is_owner as is_owner
from app.web.templates_engine import templates

router = APIRouter(
    tags=["settings"], dependencies=[Depends(current_user_required)]
)
log = get_logger("persona.chat.prompt_settings")


async def _scope(session: SessionRecord) -> int | None:
    """``None`` для владельца (глобальный kv), иначе id участника.

    Сбой резолва владельца трактуем как «участник»: хуже показать свой
    дефолтный промпт, чем чужой личный.
    """
    uid = session["user_id"]
    try:
        owner = await is_owner(uid)
    except Exception:  # noqa: BLE001 — сбой гейта → не владелец
        owner = False
    return None if owner else int(uid)


async def _render(
    request: Request, scope: int | None, *, saved: bool
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "system_prompt_settings.html",
        {
            "title": "Системный промпт",
            "active_nav": "settings",
            "presets": PRESETS,
            "active_text": await get_active_system_prompt(scope),
            "is_custom": await is_custom_system_prompt(scope),
            "default_text": DEFAULT_SYSTEM_PROMPT,
            "is_owner": scope is None,
            "saved": saved,
        },
    )


@router.get("/settings/system-prompt", response_class=HTMLResponse)
async def system_prompt_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return await _render(request, await _scope(session), saved=False)


@router.post("/settings/system-prompt", response_class=HTMLResponse, response_model=None)
async def system_prompt_save(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    prompt_text: str = Form(default=""),
) -> HTMLResponse:
    scope = await _scope(session)
    body = (prompt_text or "").strip()
    # Empty (or identical to default) → treat as reset so the user always
    # has a clean way back to ground truth.
    if body and body != DEFAULT_SYSTEM_PROMPT.strip():
        await set_active_system_prompt(body, scope)
        log.info("chat.system_prompt.saved", length=len(body), owner=scope is None)
    else:
        await reset_active_system_prompt(scope)
        log.info("chat.system_prompt.reset_via_save", owner=scope is None)
    return await _render(request, scope, saved=True)


@router.post("/settings/system-prompt/reset", response_model=None)
async def system_prompt_reset(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    scope = await _scope(session)
    await reset_active_system_prompt(scope)
    log.info("chat.system_prompt.reset", owner=scope is None)
    return RedirectResponse(url="/settings/system-prompt", status_code=303)
