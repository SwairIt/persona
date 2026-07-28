"""Authenticated HTTP adapter for the outbound-only browser PC worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.adapters.remote_browser.credentials import validate_browser_worker_token
from app.adapters.remote_browser.repository import (
    MAX_ERROR_CHARS,
    MAX_RESULT_BYTES,
    RemoteBrowserJobStateError,
    SqliteRemoteBrowserJobs,
    validate_worker_id,
)
from app.browse.agent.manager import browser_network_policy

router = APIRouter(tags=["remote-browser-worker"])
jobs = SqliteRemoteBrowserJobs()

_AGENT_PATH = (
    Path(__file__).resolve().parents[3] / "ops" / "persona_remote_browser_worker.py"
)
_MAX_REQUEST_BYTES: Final[int] = MAX_RESULT_BYTES + 16_384
_MAX_WAIT_SECONDS: Final[float] = 60.0
_CROSS_PROCESS_POLL_SECONDS: Final[float] = 2.0


async def _require_worker(token: str | None) -> None:
    if not await validate_browser_worker_token(token or ""):
        raise HTTPException(status_code=401, detail="invalid browser worker token")


async def _read_json_object(request: Request) -> dict[str, Any]:
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > _MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="request body is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request body is too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        value = json.loads(raw or b"{}")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="body must be valid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return value


@router.get("/api/llm/worker/browser/agent.py")
async def remote_browser_agent() -> PlainTextResponse:
    """Return public worker code; credentials are never embedded in it."""
    try:
        source = _AGENT_PATH.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(status_code=404, detail="browser worker script not found") from None
    return PlainTextResponse(
        source,
        media_type="text/x-python; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/llm/worker/browser/probe")
async def remote_browser_probe(
    x_worker_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    await _require_worker(x_worker_token)
    network_policy = await browser_network_policy()
    return JSONResponse(
        {
            "ok": True,
            "protocol": 1,
            "actions": [
                "open",
                "click",
                "type",
                "read",
                "screenshot",
                "close",
                "ping",
            ],
            "max_result_bytes": MAX_RESULT_BYTES,
            "network_policy": network_policy,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/llm/worker/browser/next")
async def remote_browser_next(
    x_worker_token: Annotated[str | None, Header()] = None,
    worker_id: Annotated[str, Query(min_length=1, max_length=96)] = "",
    wait: Annotated[int, Query(ge=0, le=60)] = 25,
) -> Response:
    await _require_worker(x_worker_token)
    try:
        worker = validate_worker_id(worker_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await jobs.touch_worker(worker)
    network_policy = await browser_network_policy()

    wait_seconds = min(float(wait), _MAX_WAIT_SECONDS)
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        job = await jobs.claim(worker)
        if job is not None:
            return JSONResponse(
                {
                    "job_id": job.id,
                    "owner_user_id": job.owner_user_id,
                    "session_id": job.session_id,
                    "profile_key": job.profile_key,
                    "resume_url": job.resume_url,
                    "action": job.action.name,
                    "arguments": job.action.arguments,
                    "lease_seconds": 90,
                    "network_policy": network_policy,
                },
                headers={"Cache-Control": "no-store"},
            )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return Response(status_code=204)
        await jobs.wait_for_pending(min(remaining, _CROSS_PROCESS_POLL_SECONDS))


@router.post("/api/llm/worker/browser/{job_id}/heartbeat")
async def remote_browser_heartbeat(
    job_id: int,
    request: Request,
    x_worker_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    await _require_worker(x_worker_token)
    body = await _read_json_object(request)
    unknown = set(body) - {"worker_id"}
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported fields: {', '.join(sorted(unknown))}",
        )
    try:
        worker = validate_worker_id(str(body.get("worker_id") or ""))
        cancel_requested = await jobs.heartbeat(job_id, worker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RemoteBrowserJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(
        {"ok": True, "cancel_requested": cancel_requested},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/llm/worker/browser/{job_id}/done")
async def remote_browser_done(
    job_id: int,
    request: Request,
    x_worker_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    await _require_worker(x_worker_token)
    body = await _read_json_object(request)
    allowed = {"worker_id", "result", "error"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported fields: {', '.join(sorted(unknown))}",
        )
    try:
        worker = validate_worker_id(str(body.get("worker_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    error_raw = body.get("error")
    result_raw = body.get("result")
    if error_raw is not None and result_raw is not None:
        raise HTTPException(status_code=422, detail="send result or error, not both")
    if error_raw is not None:
        if (
            not isinstance(error_raw, str)
            or not error_raw
            or len(error_raw) > MAX_ERROR_CHARS
        ):
            raise HTTPException(
                status_code=422,
                detail=f"error must be a non-empty string <= {MAX_ERROR_CHARS} characters",
            )
        result = None
        error = error_raw
    else:
        if not isinstance(result_raw, dict):
            raise HTTPException(status_code=422, detail="result must be an object")
        result = result_raw
        error = None
    try:
        status = await jobs.finish(
            job_id,
            worker,
            result=result,
            error=error,
        )
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except RemoteBrowserJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(
        {"ok": True, "status": status},
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["jobs", "router"]
