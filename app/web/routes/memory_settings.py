"""Редактор личной памяти ассистента — /settings/memory.

Trust-фича (как у Hermes): пользователь видит ВСЁ, что ИИ о нём помнит, и может
редактировать/удалять/закреплять. Данные — таблица user_memory (app/chat/user_memory.py).
Server-rendered + form-POST, работает без JS.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
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

# Жёсткие ссылки на фоновые задачи прогона «сна»: без них незавершённую
# задачу может собрать GC (asyncio держит лишь WEAK-ссылку на запущенную
# create_task). Колбэк на завершение убирает задачу из множества.
_bg_tasks: set[asyncio.Task] = set()

# Мягкий бюджет (инсайт Hermes: видимый лимит дисциплинирует, не даёт свалке расти).
_BUDGET = 60

# Согласованный набор валидных значений recall_mode по ВСЕМ страницам настроек.
# Эта страница показывает keyword/hybrid/vector/generative, а /settings/advanced —
# off/keyword/smart. Раньше валидаторы расходились: значение, сохранённое на advanced
# (off/smart), считалось здесь невалидным и тихо сбрасывалось в "". Объединяем оба
# набора, чтобы любое легитимно сохранённое значение оставалось валидным везде.
# Пустая строка "" (= авто) валидна отдельно (см. fallback в memory_engine_save).
_RECALL_MODES = ("keyword", "hybrid", "vector", "generative", "off", "smart")

# kv-настройки движка памяти (recall + ночной «сон») с дефолтами.
_ENGINE_KEYS: tuple[tuple[str, str], ...] = (
    ("recall_mode", ""),          # "" = авто (keyword/hybrid по наличию sqlite-vec)
    ("dream_enabled", "0"),       # ночная рефлексия — OPT-IN
    ("dream_hour_local", "3"),    # час «сна»
    ("recall_w_recency", "1.0"),  # веса scoring (режим generative)
    ("recall_w_importance", "1.0"),
    ("recall_w_relevance", "1.0"),
    ("recall_use_salience", "0"),  # salience-ранжирование ВО ВСЕХ режимах — OPT-IN
)

# kv-флаги ручного прогона ночного «сна» (кнопка «Обучить память сейчас»).
_TRAIN_KEYS: tuple[str, ...] = (
    "train_in_progress",   # "1" пока цикл идёт, иначе "0"
    "train_last_started",  # ISO-метка старта последнего прогона
    "train_last_finished", # ISO-метка завершения
    "train_last_result",   # человекочитаемый итог/ошибка
)


async def _require_owner(session: SessionRecord) -> None:
    """Ручной запуск «сна» — только владелец (доступ к скриншотам/аудио/чату)."""
    if not await is_owner(session["user_id"]):
        raise HTTPException(status_code=403, detail="owner only")


async def _load_train() -> dict[str, str]:
    """Текущее состояние ручного прогона (тихий fallback на пустые значения)."""
    out: dict[str, str] = {k: "" for k in _TRAIN_KEYS}
    out["train_in_progress"] = "0"
    try:
        async with get_connection() as conn:
            for key in _TRAIN_KEYS:
                val = await get_kv(conn, key)
                if val is not None:
                    out[key] = str(val)
    except Exception as exc:  # noqa: BLE001
        log.debug("memory.train_load_failed", error=str(exc))
    return out


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
    owner = await is_owner(user_id)
    # Движок памяти и результат ручного прогона — ГЛОБАЛЬНОЕ состояние
    # владельца (train_last_result — свободный текст с деталями его прогона).
    # Участнику не грузим вовсе: не только не рисуем, но и в контекст не кладём.
    engine = await _load_engine() if owner else {k: d for k, d in _ENGINE_KEYS}
    train = await _load_train() if owner else {k: "" for k in _TRAIN_KEYS}
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
            "train": train,
            "is_owner": owner,
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
    recall_use_salience: str = Form(default=""),
) -> RedirectResponse:
    """Сохранить настройки движка памяти: режим recall, веса scoring и ночной «сон».

    ТОЛЬКО ВЛАДЕЛЕЦ: все ключи здесь — ИНСТАНС-ГЛОБАЛЬНЫЕ (ночной воркер, веса
    scoring, режим recall общий для фоновых задач). Раньше хватало логина, и
    любой участник этой формой переключал владельцу «сон» и движок памяти.
    """
    await _require_owner(session)
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
        await set_kv(conn, "recall_use_salience", "1" if recall_use_salience else "0")
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


# ── Ручной запуск ночного «сна» (кнопка «Обучить память сейчас») ──────────────


async def _set_train_kv(**kv: str) -> None:
    """Записать набор train_*-флагов одной транзакцией (best-effort)."""
    try:
        async with get_connection() as conn:
            for key, val in kv.items():
                await set_kv(conn, key, val)
            await conn.commit()
    except Exception as exc:  # noqa: BLE001 — не валим фоновый прогон из-за БД
        log.debug("memory.train_kv_failed", error=str(exc))


async def _claim_train_slot() -> bool:
    """Атомарно «занять» прогон: вернуть True, если слот был свободен и теперь
    наш, False — если прогон уже идёт.

    Анти-TOCTOU: вместо «прочитал → проверил → записал» (между чтением и записью
    второй запрос успевал стартовать дубль) делаем атомарный UPDATE с условием
    ``value != '1'`` и смотрим ``rowcount``. set_kv() rowcount не отдаёт (это
    upsert), поэтому идём прямым conn.execute. Сначала INSERT OR IGNORE гарантирует
    наличие строки (на первом запуске её ещё нет — иначе UPDATE матчил бы 0 строк).
    """
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO kv_settings(key, value, updated_at) "
                "VALUES('train_in_progress', '0', datetime('now'))"
            )
            cur = await conn.execute(
                "UPDATE kv_settings SET value = '1', updated_at = datetime('now') "
                "WHERE key = 'train_in_progress' AND value != '1'"
            )
            await conn.commit()
            return (cur.rowcount or 0) > 0
    except Exception as exc:  # noqa: BLE001 — сбой БД → не даём стартовать (безопаснее)
        log.warning("memory.train_claim_failed", error=str(exc))
        return False


def _on_train_done(task: asyncio.Task) -> None:
    """Колбэк завершения фоновой задачи: снять жёсткую ссылку и не потерять
    исключение (иначе оно молча проглатывается до GC задачи)."""
    _bg_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("memory.train_task_crashed", error=str(exc))


async def _run_train(user_id: int) -> None:
    """Фоновый прогон run_dream_cycle: пишет понятный итог в train_last_result,
    всегда сбрасывает train_in_progress (даже при сбое Ollama/исключении)."""
    from datetime import datetime, timezone

    from app.chat.reflection import run_dream_cycle  # noqa: PLC0415

    result_msg = ""
    try:
        result = await run_dream_cycle(user_id)
        status = (result or {}).get("status")
        if status == "ok":
            result_msg = (
                f"Готово: кандидатов {result.get('candidates', 0)}, "
                f"запомнено {result.get('promoted', 0)}"
                + (", записан ночной инсайт" if result.get("dream") else "")
            )
        elif status == "quiet":
            result_msg = "Отложено: недавно была активность в чате, попробуй позже."
        elif status == "no_data":
            result_msg = "Нет материала или модель недоступна — запоминать нечего."
        else:
            result_msg = f"Завершено со статусом: {status or 'неизвестно'}"
    except Exception as exc:  # noqa: BLE001 — Ollama/туннель лёг и т.п.
        # Подробности (URL/IP/путь туннеля) — только в server-side лог; пользователю
        # отдаём обобщённый текст, чтобы не светить инфраструктуру через UI.
        log.warning("memory.train_failed", user_id=user_id, error=str(exc))
        result_msg = "Ошибка подключения к LLM"
    finally:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # finally НЕ должен затирать исходную ошибку прогона, если БД легла на
        # сбросе флага — глушим сбой записи отдельно.
        try:
            await _set_train_kv(
                train_in_progress="0",
                train_last_finished=now,
                train_last_result=result_msg or "Прогон завершён.",
            )
        except Exception as exc:  # noqa: BLE001 — сбой БД в finally не валит задачу
            log.warning("memory.train_finalize_failed", user_id=user_id, error=str(exc))


@router.post("/settings/memory/train", response_model=None)
async def memory_train(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse | HTMLResponse:
    """Запустить ночной прогон «сна» вручную, ФОНОМ (не блокируя HTTP-ответ)."""
    await _require_owner(session)
    from datetime import datetime, timezone

    # Анти-дубль БЕЗ TOCTOU: атомарно «занимаем» слот одним UPDATE с условием.
    # Если слот уже занят (прогон идёт) — claim вернёт False, второй прогон не стартуем.
    if not await _claim_train_slot():
        return await _render(
            request, session["user_id"], saved="Прогон уже идёт — дождись завершения."
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Метка старта/очистка прошлого результата (флаг уже выставлен в '1' атомарно).
    await _set_train_kv(
        train_last_started=now,
        train_last_result="",
    )
    # Фоновая задача: HTTP-ответ возвращается сразу, цикл крутится отдельно.
    # Держим жёсткую ссылку (см. _bg_tasks), иначе GC может убить задачу до конца.
    task = asyncio.create_task(_run_train(session["user_id"]))
    _bg_tasks.add(task)
    task.add_done_callback(_on_train_done)
    return RedirectResponse("/settings/memory", status_code=303)


@router.get("/settings/memory/train/status", response_model=None)
async def memory_train_status(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """JSON-состояние ручного прогона (для авто-обновления кнопки на странице)."""
    await _require_owner(session)
    train = await _load_train()
    return JSONResponse(
        {
            "in_progress": train.get("train_in_progress") == "1",
            "last_started": train.get("train_last_started") or "",
            "last_finished": train.get("train_last_finished") or "",
            "result": train.get("train_last_result") or "",
        }
    )
