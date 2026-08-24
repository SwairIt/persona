"""Панель «ИИ» в переписке — /api/messages/{id}/ai + kill-switch.

Только HTTP: весь SQL и все инварианты живут в :mod:`app.social.ai_pref`
и :mod:`app.social.ai_reply` (архитектурный гейт запрещает роутам прямой
доступ к БД).

Три вещи, которые этот файл обязан держать:

1. ``thread_id`` из URL НИКОГДА не используется напрямую — сначала
   ``thread_header`` резолвит доступ (тот же ``_require_thread_member``,
   что и у остальной переписки) и отдаёт настоящего собеседника. Настройка
   ключуется по нему, а не по номеру ветки из адресной строки.
2. Черновик отдаётся ТОЛЬКО из-под сессии его владельца — ключ выборки
   (user_id, thread_id) собирается из сессии, а не из тела запроса.
3. Включение ``auto`` требует явного ``ack`` в теле. Без него слой хранения
   сам опустит режим до ``draft`` (см. ``save_pref``) — здесь мы просто
   честно возвращаем то, что реально сохранилось.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.llm.client import user_llm_configured
from app.logging_setup import get_logger
from app.social import ai_pref
from app.social.repository import ThreadAccessError, thread_header

router = APIRouter(tags=["social"])
log = get_logger("persona.social.dm_ai")

_NOT_FOUND = {"error": "переписка не найдена"}


def _pref_json(pref: dict[str, Any]) -> dict[str, Any]:
    """Наружу отдаём только то, что нужно UI (без служебных полей)."""
    return {
        "mode": pref["mode"],
        "style_note": pref["style_note"],
        "quota_daily": pref["quota_daily"],
        "used_today": pref["used_today"],
        "auto_ack": pref["auto_ack"],
        "last_error": pref["last_error"],
    }


@router.get("/api/messages/{thread_id}/ai", response_class=JSONResponse)
async def api_dm_ai_get(
    thread_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Настройка ИИ для ЭТОЙ переписки + мой черновик (если он есть).

    Черновик виден только здесь и только владельцу: в ``dm_message`` он не
    попадает вовсе, поэтому poll собеседника его не увидит физически.
    """
    uid = int(session["user_id"])
    try:
        header = await thread_header(thread_id, uid)
    except ThreadAccessError:
        return JSONResponse(_NOT_FOUND, status_code=404)
    pref = await ai_pref.get_pref(uid, header["other_id"])
    draft = await ai_pref.get_draft(uid, thread_id)
    return JSONResponse(
        {
            "pref": _pref_json(dict(pref)),
            "draft": draft,
            "llm_ready": await user_llm_configured(uid),
            "limits": {
                "max_quota": ai_pref.MAX_QUOTA_DAILY,
                "min_interval_seconds": ai_pref.MIN_INTERVAL_SECONDS,
                "max_style_chars": ai_pref.MAX_STYLE_NOTE_CHARS,
            },
        }
    )


@router.post("/api/messages/{thread_id}/ai", response_class=JSONResponse)
async def api_dm_ai_set(
    thread_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """Сохранить режим/стиль/квоту для ЭТОЙ переписки."""
    uid = int(session["user_id"])
    try:
        header = await thread_header(thread_id, uid)
    except ThreadAccessError:
        return JSONResponse(_NOT_FOUND, status_code=404)

    raw_quota = payload.get("quota_daily", ai_pref.DEFAULT_QUOTA_DAILY)
    try:
        quota = int(raw_quota)
    except (TypeError, ValueError):
        quota = ai_pref.DEFAULT_QUOTA_DAILY

    pref = await ai_pref.save_pref(
        uid,
        header["other_id"],
        mode=str(payload.get("mode") or "off"),
        style_note=str(payload.get("style_note") or ""),
        quota_daily=quota,
        auto_ack=bool(payload.get("ack")),
    )
    if pref["mode"] == "off":
        # Выключили — черновик больше не наш случай.
        await ai_pref.clear_draft(uid, thread_id)
    log.info("social.dm_ai.saved", user_id=uid, mode=pref["mode"])
    return JSONResponse({"ok": True, "pref": _pref_json(dict(pref))})


@router.post("/api/messages/{thread_id}/ai/dismiss", response_class=JSONResponse)
async def api_dm_ai_dismiss(
    thread_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """«Убрать» предложенный черновик (и после ручной отправки — тоже)."""
    uid = int(session["user_id"])
    try:
        await thread_header(thread_id, uid)
    except ThreadAccessError:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": await ai_pref.clear_draft(uid, thread_id)})


@router.post("/api/messages/ai/off-everywhere", response_class=JSONResponse)
async def api_dm_ai_off_everywhere(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Глобальный рубильник: выключить ИИ во ВСЕХ моих переписках.

    Один POST без параметров: у аварийного выключателя не должно быть
    вариантов «а в этой оставить».
    """
    uid = int(session["user_id"])
    changed = await ai_pref.disable_everywhere(uid)
    log.info("social.dm_ai.off_everywhere", user_id=uid, changed=changed)
    return JSONResponse({"ok": True, "changed": changed})


__all__ = ["router"]
