"""HTTP-эндпоинты «Persona LLM Worker» (срез W-A).

ПК-воркер делает ИСХОДЯЩИЕ запросы сюда (long-poll), забирает задачи из очереди
в БД, считает на локальной Ollama и шлёт ответ обратно. Всё чистый HTTP — без
WebSocket, дружит с FastPanel-прокси.

Авторизация воркера = заголовок ``X-Worker-Token`` → validate_worker_token
(агент без cookie, поэтому НЕ current_user). Owner-эндпоинты (rotate-token,
status) — наоборот, через current_user_required + is_owner.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.llm import worker_queue

router = APIRouter(tags=["llm-worker"])

# Шаг опроса очереди внутри long-poll — мягкий, чтобы не жечь loop.
_POLL_STEP_SECONDS = 0.3
# Потолок ожидания клиента, чтобы один воркер не висел вечно.
_MAX_WAIT_SECONDS = 60.0


async def _require_worker(token: str | None) -> None:
    """Гард воркера: 401, если нет/неверный X-Worker-Token."""
    if not await worker_queue.validate_worker_token(token or ""):
        raise HTTPException(status_code=401, detail="Неверный токен воркера")


async def _require_owner(user: SessionRecord) -> None:
    """Гард владельца: 403, если запрос не от владельца инстанса."""
    if not await is_owner(int(user["user_id"])):
        raise HTTPException(status_code=403, detail="Только владелец")


@router.get("/api/llm/worker/next")
async def worker_next(
    x_worker_token: Annotated[str | None, Header()] = None,
    wait: Annotated[int, Query()] = 25,
    worker_id: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
) -> Response:
    """Long-poll: вернуть следующую задачу или 204 по таймауту.

    Сначала отмечаем воркер живым (touch_worker), затем циклически пытаемся
    атомарно забрать pending-задачу (claim_next) с шагом ~0.3с до ``wait`` сек.
    Есть задача → 200 {job_id,kind,model,payload}; иначе 204.
    """
    await _require_worker(x_worker_token)

    wid = (worker_id or "").strip() or "worker"
    await worker_queue.touch_worker(wid, (model or "").strip() or None)

    deadline = max(0.0, min(float(wait), _MAX_WAIT_SECONDS))
    waited = 0.0
    while True:
        job = await worker_queue.claim_next(wid)
        if job is not None:
            return JSONResponse(
                {
                    "job_id": job["id"],
                    "kind": job["kind"],
                    "model": job["model"],
                    "payload": job["payload"],
                }
            )
        if waited >= deadline:
            return Response(status_code=204)
        # async sleep — не блокируем event loop, пока ждём задачу.
        await asyncio.sleep(_POLL_STEP_SECONDS)
        waited += _POLL_STEP_SECONDS


@router.post("/api/llm/worker/{job_id}/chunk")
async def worker_chunk(
    job_id: int,
    x_worker_token: Annotated[str | None, Header()] = None,
    body: Annotated[dict, Body()] = ...,  # type: ignore[assignment]
) -> JSONResponse:
    """Принять стрим-чанк ответа от воркера: {seq:int, content:str}."""
    await _require_worker(x_worker_token)
    seq = int(body.get("seq", 0))
    content = str(body.get("content", ""))
    await worker_queue.add_chunk(job_id, seq, content)
    return JSONResponse({"ok": True})


@router.post("/api/llm/worker/{job_id}/done")
async def worker_done(
    job_id: int,
    x_worker_token: Annotated[str | None, Header()] = None,
    body: Annotated[dict, Body()] = ...,  # type: ignore[assignment]
) -> JSONResponse:
    """Завершить задачу: {error?:str, result?:str}.

    result используется для embed-задач (JSON-вектор); error — текст исключения.
    """
    await _require_worker(x_worker_token)
    error = body.get("error")
    result = body.get("result")
    await worker_queue.finish_job(
        job_id,
        error=str(error) if error else None,
        result=str(result) if result is not None else None,
    )
    return JSONResponse({"ok": True})


@router.post("/api/llm/worker/rotate-token")
async def worker_rotate_token(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Сгенерить новый токен воркера (OWNER-only). Плейнтекст — один раз."""
    await _require_owner(user)
    token = await worker_queue.rotate_worker_token()
    return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})


@router.get("/api/llm/worker/status")
async def worker_status(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Статус воркера {online,model,last_seen} (OWNER-only)."""
    await _require_owner(user)
    status = await worker_queue.worker_status()
    return JSONResponse(status, headers={"Cache-Control": "no-store"})
