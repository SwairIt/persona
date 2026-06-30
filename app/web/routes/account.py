"""Лёгкий эндпоинт для аккаунт-виджета в шапке (правый верхний угол).

base.html рендерится на каждой странице, а email/роль/статус воркера НЕ являются
глобальными Jinja-переменными (их не прокидывает каждый роут). Поэтому виджет
тянет данные клиентски отсюда — один маленький JSON, работает на любой странице.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord

router = APIRouter(tags=["account"])


@router.get("/api/account.json")
async def account_json(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Кто залогинен + роль + статус ИИ-воркера (для аккаунт-чипа в шапке)."""
    uid = int(session["user_id"])
    owner = await is_owner(uid)

    worker_online = False
    worker_model: str | None = None
    if owner:
        # Статус ПК-воркера показываем только владельцу (его инфраструктура).
        try:
            from app.llm.worker_queue import worker_status  # noqa: PLC0415

            st = await worker_status()
            worker_online = bool(st.get("online"))
            worker_model = st.get("model")
        except Exception:  # noqa: BLE001 — воркер опционален, не валим виджет
            pass

    return JSONResponse(
        {
            "email": session.get("email") or "",
            "is_owner": owner,
            "worker_online": worker_online,
            "worker_model": worker_model,
        },
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
