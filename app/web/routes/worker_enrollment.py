"""Owner-issued, one-use enrollment for the outbound PC workers.

The public exchange endpoint deliberately lives below ``/api/llm/worker/``:
the auth middlewares already allow the not-yet-enrolled worker to reach that
namespace.  Possession of an existing worker credential is neither accepted
nor required.
"""

from __future__ import annotations

import json
from typing import Any, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.adapters.remote_browser.repository import validate_worker_id
from app.adapters.worker_enrollment import SqliteWorkerEnrollment
from app.application.worker_enrollment import (
    EnrollmentError,
    EnrollmentIssue,
    WorkerEnrollmentService,
)
from app.audit import log_action
from app.auth.owner import is_primary_owner
from app.auth.sessions import SessionRecord  # noqa: TC001
from app.llm import worker_queue
from app.web import rate_limit

router = APIRouter(tags=["worker-enrollment"])

_MAX_BODY_BYTES: Final[int] = 2_048
_RATE_WINDOW_SECONDS: Final[int] = 60
_RATE_GLOBAL_MAX: Final[int] = 240
_RATE_IP_MAX: Final[int] = 30
_NO_STORE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
}
_service = WorkerEnrollmentService(SqliteWorkerEnrollment())


async def _read_small_json(request: Request) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > _MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="request body is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body is too large")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks) or b"{}")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="body must be valid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return value


def _require_json(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be application/json",
        )


def _same_origin_or_trusted_local(request: Request) -> None:
    origin = request.headers.get("origin", "").strip()
    if origin:
        parsed = urlsplit(origin)
        expected_port = request.url.port
        actual_port = parsed.port
        if (
            parsed.scheme.lower() != request.url.scheme.lower()
            or (parsed.hostname or "").lower() != (request.url.hostname or "").lower()
            or actual_port != expected_port
        ):
            raise HTTPException(status_code=403, detail="same-origin request required")
        return
    peer = request.client.host if request.client else ""
    if peer not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail="Origin header required")


def _public_rate_allowed(request: Request) -> bool:
    peer = request.client.host if request.client else "unknown"
    if not rate_limit.allow(
        f"worker-enrollment:public:ip:{peer}",
        _RATE_IP_MAX,
        _RATE_WINDOW_SECONDS,
    ):
        return False
    return rate_limit.allow(
        "worker-enrollment:public:global",
        _RATE_GLOBAL_MAX,
        _RATE_WINDOW_SECONDS,
    )


async def _validated_runtime_config() -> tuple[str, str]:
    runtime = await worker_queue.worker_runtime_config()
    chat_model = str(runtime.get("chat_model") or "").strip()
    embedding_model = str(runtime.get("embedding_model") or "").strip()
    for value in (chat_model, embedding_model):
        if not value or len(value) > 200 or "\r" in value or "\n" in value:
            raise RuntimeError("worker runtime model configuration is invalid")
    return chat_model, embedding_model


def _worker_id(value: object) -> str:
    try:
        return validate_worker_id(str(value or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require_confidential_transport(request: Request) -> None:
    """Reject credential exchange over cleartext except on the local machine."""
    if request.url.scheme == "https":
        return
    if request.url.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    raise HTTPException(
        status_code=400,
        detail="worker enrollment exchange requires HTTPS",
    )


async def issue_worker_enrollment_for_owner(
    session: SessionRecord,
    request: Request,
    body: dict[str, Any],
) -> JSONResponse:
    """Issue a five-minute ticket to the authenticated primary owner only."""
    user_id = int(session["user_id"])
    if not await is_primary_owner(user_id):
        raise HTTPException(status_code=403, detail="primary owner access required")
    _require_json(request)
    _same_origin_or_trusted_local(request)
    unknown = set(body) - {"action", "expected_worker_id"}
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported fields: {', '.join(sorted(unknown))}",
        )
    expected = body.get("expected_worker_id")
    expected_worker_id = _worker_id(expected) if expected is not None else None
    issued = await _service.issue(
        EnrollmentIssue(
            owner_user_id=user_id,
            is_primary_owner=True,
            expected_worker_id=expected_worker_id,
        )
    )
    await log_action(
        "worker.enrollment.issue",
        actor=f"user:{user_id}",
        target=f"ticket:{issued.ledger_id}",
        detail=(
            "capability=llm+browser "
            f"expires_at={issued.expires_at.isoformat()} "
            f"worker_bound={expected_worker_id is not None}"
        ),
    )
    return JSONResponse(
        {
            "ok": True,
            "ticket": issued.ticket,
            "expires_at": issued.expires_at.isoformat(),
            "capability": "llm+browser",
            "expected_worker_id": issued.expected_worker_id,
        },
        headers=_NO_STORE_HEADERS,
    )


@router.post("/api/llm/worker/enrollment")
async def public_worker_enrollment(request: Request) -> JSONResponse:
    """Exchange a ticket or activate durably stored pending credentials."""
    _require_confidential_transport(request)
    _require_json(request)
    if not _public_rate_allowed(request):
        return JSONResponse(
            {"ok": False, "error": "too many enrollment requests"},
            status_code=429,
            headers={**_NO_STORE_HEADERS, "Retry-After": str(_RATE_WINDOW_SECONDS)},
        )
    body = await _read_small_json(request)
    phase = str(body.get("phase") or "")
    if phase == "exchange":
        allowed = {"phase", "ticket", "worker_id"}
    elif phase == "activate":
        allowed = {
            "phase",
            "enrollment_id",
            "worker_id",
            "llm_worker_token",
            "browser_worker_token",
        }
    else:
        raise HTTPException(status_code=422, detail="phase must be exchange or activate")
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported fields: {', '.join(sorted(unknown))}",
        )
    worker_id = _worker_id(body.get("worker_id"))
    if phase == "activate":
        raw_id = body.get("enrollment_id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            raise HTTPException(status_code=422, detail="invalid enrollment_id")
        try:
            activation = await _service.activate(
                ledger_id=raw_id,
                worker_id=worker_id,
                llm_worker_token=str(body.get("llm_worker_token") or ""),
                browser_worker_token=str(body.get("browser_worker_token") or ""),
            )
        except EnrollmentError as exc:
            if exc.known_ticket:
                await log_action(
                    "worker.enrollment.activate",
                    actor=f"worker:{worker_id}",
                    target=f"ticket:{raw_id}",
                    detail=f"result={exc.reason}",
                    success=False,
                )
            raise HTTPException(
                status_code=401,
                detail="invalid or expired pending enrollment",
                headers=_NO_STORE_HEADERS,
            ) from None
        await log_action(
            "worker.enrollment.activate",
            actor=f"worker:{worker_id}",
            target=f"ticket:{activation.ledger_id}",
            detail=(
                "capability=llm+browser "
                f"already_active={activation.already_active}"
            ),
        )
        return JSONResponse(
            {
                "ok": True,
                "phase": "activated",
                "worker_id": activation.worker_id,
                "enrollment_id": activation.ledger_id,
                "activated_at": activation.activated_at.isoformat(),
                "already_active": activation.already_active,
            },
            headers=_NO_STORE_HEADERS,
        )

    chat_model, embedding_model = await _validated_runtime_config()
    try:
        credentials = await _service.exchange(
            str(body.get("ticket") or ""),
            worker_id,
        )
    except EnrollmentError as exc:
        if exc.known_ticket:
            await log_action(
                "worker.enrollment.exchange",
                actor=f"worker:{worker_id}",
                detail=f"result={exc.reason}",
                success=False,
            )
        # Do not turn ticket state into a public validity oracle.
        raise HTTPException(
            status_code=401,
            detail="invalid or expired enrollment ticket",
            headers=_NO_STORE_HEADERS,
        ) from None
    await log_action(
        "worker.enrollment.exchange",
        actor=f"worker:{worker_id}",
        target=f"ticket:{credentials.ledger_id}",
        detail="capability=llm+browser result=pending_activation",
    )
    return JSONResponse(
        {
            "ok": True,
            "phase": "pending_activation",
            "worker_id": credentials.worker_id,
            "enrollment_id": credentials.ledger_id,
            "activation_expires_at": credentials.activation_expires_at.isoformat(),
            "llm_worker_token": credentials.llm_worker_token,
            "browser_worker_token": credentials.browser_worker_token,
            "chat_model": chat_model,
            "embedding_model": embedding_model,
        },
        headers=_NO_STORE_HEADERS,
    )


__all__ = ["issue_worker_enrollment_for_owner", "router"]
