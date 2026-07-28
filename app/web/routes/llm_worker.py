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
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.llm import worker_queue

router = APIRouter(tags=["llm-worker"])

# Исходник ПК-агента (для self-host one-command установки). app/web/routes → repo root.
_AGENT_PY = Path(__file__).resolve().parents[3] / "ops" / "persona_llm_worker.py"
_BOOTSTRAP_PS1 = (
    Path(__file__).resolve().parents[3] / "ops" / "persona_llm_pc_bootstrap.ps1"
)

# Публичный one-shot установщик для PowerShell: поднимает Ollama+модели, ставит
# httpx, качает агент с сайта и крутит его с авто-рестартом. Токен и адрес —
# из env ($env:PERSONA_WORKER_TOKEN обязателен; PERSONA_SERVER опц.). ASCII-only,
# чтобы безопасно пройти через `irm ... | iex`.
_INSTALL_PS1 = r"""$ErrorActionPreference = 'Continue'
$server = if ($env:PERSONA_SERVER) { $env:PERSONA_SERVER } else { 'https://persona.getdoday.ru' }
if (-not $env:PERSONA_WORKER_TOKEN) {
  Write-Host 'ERROR: set $env:PERSONA_WORKER_TOKEN before running this.' -ForegroundColor Red
  return
}
Write-Host '[Persona] LLM worker setup...' -ForegroundColor Cyan
if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
  Write-Host '[Persona] starting ollama serve...' -ForegroundColor Cyan
  Start-Process ollama -ArgumentList 'serve'; Start-Sleep -Seconds 3
}
Write-Host '[Persona] pulling models (gemma3:4b, nomic-embed-text)...' -ForegroundColor Cyan
ollama pull gemma3:4b
ollama pull nomic-embed-text
Write-Host '[Persona] installing httpx...' -ForegroundColor Cyan
python -m pip install -q httpx
$dir = Join-Path $env:LOCALAPPDATA 'persona-worker'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$py = Join-Path $dir 'persona_llm_worker.py'
Invoke-WebRequest -Uri "$server/api/llm/worker/agent.py" -OutFile $py -UseBasicParsing
$env:PERSONA_SERVER = $server
Write-Host "[Persona] worker running -> $server (Ctrl+C to stop)" -ForegroundColor Green
while ($true) {
  try { python $py } catch { }
  Write-Host '[Persona] worker exited, restarting in 3s...' -ForegroundColor Yellow
  Start-Sleep -Seconds 3
}
"""


@router.get("/api/llm/worker/agent.py")
async def worker_agent_py() -> PlainTextResponse:
    """Публично отдаёт исходник ПК-агента (не секрет — токен задаёт пользователь)."""
    try:
        src = _AGENT_PY.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="agent script not found") from None
    return PlainTextResponse(src, media_type="text/x-python; charset=utf-8")


@router.get("/api/llm/worker/install.ps1")
async def worker_install_ps1() -> PlainTextResponse:
    """Публичный one-shot установщик: `irm <site>/api/llm/worker/install.ps1 | iex`."""
    return PlainTextResponse(_INSTALL_PS1, media_type="text/plain; charset=utf-8")


@router.get("/api/llm/worker/bootstrap.ps1")
async def worker_bootstrap_ps1() -> PlainTextResponse:
    """Public self-contained Windows bootstrap for the dedicated LLM PC."""
    try:
        src = _BOOTSTRAP_PS1.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="bootstrap script not found") from None
    return PlainTextResponse(
        src,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/llm/worker/probe")
async def worker_probe(
    x_worker_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Validate connectivity and worker credentials without claiming a job."""
    await _require_worker(x_worker_token)
    config = await worker_queue.worker_runtime_config()
    return JSONResponse(
        {"ok": True, **config},
        headers={"Cache-Control": "no-store"},
    )

# Event будит long-poll сразу при enqueue в этом server process. Timeout нужен
# только как fallback при нескольких uvicorn workers / внешней записи в БД.
_CROSS_PROCESS_POLL_SECONDS = 2.0
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

    Сначала отмечаем воркер живым (touch_worker), затем атомарно пытаемся
    забрать pending-задачу. Пустая очередь ждёт process-local Event, а не
    захватывает SQLite write-lock по таймеру. Есть задача → 200, иначе 204.
    """
    await _require_worker(x_worker_token)

    wid = (worker_id or "").strip() or "worker"
    await worker_queue.touch_worker(wid, (model or "").strip() or None)

    wait_seconds = max(0.0, min(float(wait), _MAX_WAIT_SECONDS))
    deadline = asyncio.get_running_loop().time() + wait_seconds
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
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return Response(status_code=204)
        await worker_queue.wait_for_pending_job(
            min(remaining, _CROSS_PROCESS_POLL_SECONDS)
        )


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
    try:
        await worker_queue.add_chunk(job_id, seq, content)
    except worker_queue.WorkerJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    try:
        await worker_queue.finish_job(
            job_id,
            error=str(error) if error else None,
            result=str(result) if result is not None else None,
        )
    except worker_queue.WorkerJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
