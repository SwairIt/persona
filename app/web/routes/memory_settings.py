"""Редактор личной памяти ассистента — /settings/memory.

Trust-фича (как у Hermes): пользователь видит ВСЁ, что ИИ о нём помнит, и может
редактировать/удалять/закреплять. Данные — таблица user_memory (app/chat/user_memory.py).
Server-rendered + form-POST, работает без JS.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.chat.user_memory import (
    add_memory,
    count_memory,
    delete_memory,
    edit_memory,
    list_memory,
    restore_memory,
    set_pinned,
)
from app.dreams import invalidate_reflection, list_active_reflections
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.memory.settings")

# Мягкий бюджет (инсайт Hermes: видимый лимит дисциплинирует, не даёт свалке расти).
_BUDGET = 60

_RECALL_MODES = ("keyword", "hybrid", "vector", "generative")

# kv-настройки движка памяти (recall + ночной «сон») с дефолтами.
_ENGINE_KEYS: tuple[tuple[str, str], ...] = (
    ("recall_mode", ""),          # "" = авто (keyword/hybrid по наличию sqlite-vec)
    ("dream_enabled", "0"),       # ночная рефлексия — OPT-IN
    ("dream_hour_local", "3"),    # час «сна»
    ("recall_w_recency", "1.0"),  # веса scoring (режим generative)
    ("recall_w_importance", "1.0"),
    ("recall_w_relevance", "1.0"),
)


async def _load_engine() -> dict[str, str]:
    """Текущие kv-настройки движка памяти."""
    out: dict[str, str] = {k: d for k, d in _ENGINE_KEYS}
    try:
        async with get_connection() as conn:
            for key, default in _ENGINE_KEYS:
                out[key] = (await get_kv(conn, key)) or default
    except Exception as exc:  # noqa: BLE001
        log.debug("memory.engine_load_failed", error=str(exc))
    return out


async def _load_reflections(user_id: int) -> list:
    """Свежие инсайты/«сны» ночного воркера (пусто, если фича не использовалась
    или таблицы reflection ещё нет — тихий fallback)."""
    try:
        return await list_active_reflections(user_id, kinds=["insight", "dream"], limit=12)
    except Exception as exc:  # noqa: BLE001
        log.debug("memory.reflections_load_failed", error=str(exc))
        return []


async def _render(request: Request, user_id: int, *, saved: str = "") -> HTMLResponse:
    # bi-temporal: активные факты + история (устаревшие/опровергнутые) отдельно.
    all_items = await list_memory(user_id, limit=500, include_invalidated=True)
    active = [i for i in all_items if i["valid_until"] is None]
    history = [i for i in all_items if i["valid_until"] is not None]
    # карта id→text для подписи «заменено на …» (superseded_by → актуальный факт)
    by_id = {i["id"]: i["text"] for i in all_items}
    for h in history:
        sb = h.get("superseded_by")
        h["superseded_text"] = by_id.get(sb) if sb else None
    engine = await _load_engine()
    reflections = await _load_reflections(user_id)
    return templates.TemplateResponse(
        request,
        "memory_settings.html",
        {
            "title": "Память — что ИИ помнит обо мне",
            "active_nav": "settings",
            "items": active,
            "history": history,
            "count": len(active),
            "budget": _BUDGET,
            "saved": saved,
            "engine": engine,
            "reflections": reflections,
        },
    )


@router.get("/settings/memory", response_class=HTMLResponse)
async def memory_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return await _render(request, session["user_id"])


@router.post("/settings/memory/add", response_model=None)
async def memory_add(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    text: str = Form(default=""),
    kind: str = Form(default="fact"),
    pinned: str = Form(default=""),
) -> RedirectResponse:
    if text.strip():
        await add_memory(
            session["user_id"], text, kind=kind, pinned=bool(pinned)
        )
    return RedirectResponse("/settings/memory", status_code=303)


@router.post("/settings/memory/{mem_id}/delete", response_model=None)
async def memory_delete(
    mem_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    await delete_memory(session["user_id"], mem_id)
    return RedirectResponse("/settings/memory", status_code=303)


@router.post("/settings/memory/{mem_id}/pin", response_model=None)
async def memory_pin(
    mem_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    pinned: str = Form(default=""),
) -> RedirectResponse:
    await set_pinned(session["user_id"], mem_id, bool(pinned))
    return RedirectResponse("/settings/memory", status_code=303)


@router.post("/settings/memory/{mem_id}/edit", response_model=None)
async def memory_edit(
    mem_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    text: str = Form(default=""),
) -> RedirectResponse:
    if text.strip():
        await edit_memory(session["user_id"], mem_id, text)
    return RedirectResponse("/settings/memory", status_code=303)


@router.post("/settings/memory/{mem_id}/restore", response_model=None)
async def memory_restore(
    mem_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Откат soft-invalidate: вернуть устаревший/опровергнутый факт в актуальные."""
    await restore_memory(session["user_id"], mem_id)
    return RedirectResponse("/settings/memory", status_code=303)


@router.post("/settings/memory/engine", response_model=None)
async def memory_engine_save(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    recall_mode: str = Form(default=""),
    dream_enabled: str = Form(default=""),
    dream_hour_local: str = Form(default="3"),
    recall_w_recency: str = Form(default="1.0"),
    recall_w_importance: str = Form(default="1.0"),
    recall_w_relevance: str = Form(default="1.0"),
) -> RedirectResponse:
    """Сохранить настройки движка памяти: режим recall, веса scoring и ночной «сон»."""
    rm = recall_mode if recall_mode in _RECALL_MODES else ""
    try:
        hour = max(0, min(23, int(dream_hour_local)))
    except (TypeError, ValueError):
        hour = 3

    def _w(v: str) -> str:
        try:
            return str(max(0.0, min(5.0, float(v))))
        except (TypeError, ValueError):
            return "1.0"

    async with get_connection() as conn:
        await set_kv(conn, "recall_mode", rm)
        await set_kv(conn, "dream_enabled", "1" if dream_enabled else "0")
        await set_kv(conn, "dream_hour_local", str(hour))
        await set_kv(conn, "recall_w_recency", _w(recall_w_recency))
        await set_kv(conn, "recall_w_importance", _w(recall_w_importance))
        await set_kv(conn, "recall_w_relevance", _w(recall_w_relevance))
        await conn.commit()
    return RedirectResponse("/settings/memory", status_code=303)


@router.post("/settings/memory/reflection/{ref_id}/forget", response_model=None)
async def memory_reflection_forget(
    ref_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Soft-invalidate рефлексии — «забыть» инсайт ночного «сна» (остаётся в истории)."""
    await invalidate_reflection(session["user_id"], ref_id)
    return RedirectResponse("/settings/memory", status_code=303)
