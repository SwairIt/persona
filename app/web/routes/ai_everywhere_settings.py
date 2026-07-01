"""Мастер-режим «ИИ везде» — один тумблер, оживляющий весь сайт.

Когда kv ``ai_everywhere='1'`` — по всему сайту включаются ИИ-фичи (вездесущий
копилот справа снизу, ИИ-календарь, поиск настроек ИИ, саммари экранов). Дефолт
ВЫКЛ → сайт работает как обычно (фичи аддитивны).

UI-видимость фич — через Jinja-глобал ``get_ai_everywhere()`` (templates_engine).
Серверный гейт новых ИИ-эндпоинтов — через :func:`is_ai_everywhere` (async), чтобы
при OFF отдавать благородный отказ, а не считать LLM впустую.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])

_KV = "ai_everywhere"


async def is_ai_everywhere() -> bool:
    """True, если мастер-режим «ИИ везде» включён (для гейта ИИ-эндпоинтов)."""
    try:
        async with get_connection() as conn:
            return (await get_kv(conn, _KV) or "0").strip() == "1"
    except Exception:  # noqa: BLE001 — при сбое считаем выключенным (безопасно)
        return False


async def _require_owner(session: SessionRecord) -> int:
    from fastapi import HTTPException  # noqa: PLC0415

    uid = int(session["user_id"])
    if not await is_owner(uid):
        raise HTTPException(status_code=403, detail="Только владелец")
    return uid


@router.get("/settings/ai-everywhere", response_class=HTMLResponse, response_model=None)
async def ai_everywhere_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Страница мастер-тумблера «ИИ везде»."""
    await _require_owner(session)
    return templates.TemplateResponse(
        request,
        "ai_everywhere_settings.html",
        {
            "title": "ИИ везде",
            "active_nav": "ai-everywhere-settings",
            "session": session,
            "enabled": await is_ai_everywhere(),
        },
    )


@router.post("/settings/ai-everywhere", response_model=None)
async def ai_everywhere_save(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    ai_everywhere: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Сохранить тумблер (чекбокс формы → kv '1'/'0')."""
    await _require_owner(session)
    async with get_connection() as conn:
        await set_kv(conn, _KV, "1" if ai_everywhere else "0")
        await conn.commit()
    return RedirectResponse(url="/settings/ai-everywhere", status_code=303)


@router.post("/api/ai-everywhere/toggle", response_model=None)
async def ai_everywhere_toggle(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Быстрый one-click тумблер (из шапки/панели копилота). Возвращает новое состояние."""
    await _require_owner(session)
    async with get_connection() as conn:
        cur = (await get_kv(conn, _KV) or "0").strip() == "1"
        await set_kv(conn, _KV, "0" if cur else "1")
        await conn.commit()
    return JSONResponse({"enabled": not cur}, headers={"Cache-Control": "no-store"})


__all__ = ["router", "is_ai_everywhere"]
