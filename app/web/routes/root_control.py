"""Root Control Center — /root (только владелец).

Read-only пульт владельца: live-логи системы (кольцевой буфер + SSE),
сводка здоровья (воркеры/БД/аудит из health_dashboard) и быстрые ссылки на
существующие админ-страницы. НЕ управляет пользователями/ролями и НЕ трогает
auth_gate — это отдельный (рискованный) этап. Каждый хендлер заново проверяет
владельца (defence-in-depth), даже если общий гейт уже есть.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["root"])
log = get_logger("persona.root")


async def _require_owner(session: SessionRecord) -> int:
    uid = session["user_id"]
    if not await is_owner(uid):
        raise HTTPException(status_code=403, detail="только для владельца")
    return uid


@router.get("/root", response_class=HTMLResponse)
async def root_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    await _require_owner(session)
    # Сводка здоровья — best-effort, страница не должна падать из-за неё.
    health: dict = {}
    try:
        from app.health_dashboard import build_health_state  # noqa: PLC0415

        health = dict(await build_health_state())
    except Exception as exc:  # noqa: BLE001
        log.warning("root.health_failed", error=str(exc))
    users: list = []
    try:
        from app.auth.roles import list_users  # noqa: PLC0415

        users = await list_users()
    except Exception as exc:  # noqa: BLE001
        log.warning("root.users_failed", error=str(exc))
    return templates.TemplateResponse(
        request,
        "root.html",
        {
            "title": "Root — пульт владельца",
            "active_nav": "root",
            "health": health,
            "users": users,
        },
    )


@router.get("/root/db/integrity", response_class=JSONResponse)
async def root_db_integrity(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Read-only проверка целостности БД (owner-only): FK-check + quick_check.

    Никаких мутаций/VACUUM — только диагностика. Безопасно жать сколько угодно.
    """
    await _require_owner(session)
    result: dict = {"fk": None, "quick": None}
    try:
        from app.db_integrity import run_foreign_key_check  # noqa: PLC0415

        result["fk"] = await run_foreign_key_check()
    except Exception as exc:  # noqa: BLE001
        result["fk"] = {"status": "error", "error": str(exc)}
    try:
        from app.storage.db import get_connection  # noqa: PLC0415

        async with get_connection() as conn:
            cur = await conn.execute("PRAGMA quick_check")
            rows = await cur.fetchall()
        result["quick"] = [str(r[0]) for r in rows][:20]
    except Exception as exc:  # noqa: BLE001
        result["quick"] = [f"error: {exc}"]
    return JSONResponse(result)


@router.get("/root/logs/recent.json", response_class=JSONResponse)
async def root_logs_recent(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    limit: int = 300,
    level: str = "",
) -> JSONResponse:
    await _require_owner(session)
    from app.log_buffer import buffer_size, get_recent  # noqa: PLC0415

    return JSONResponse(
        {"logs": get_recent(limit=limit, level=level or None), "buffered": buffer_size()}
    )


__all__ = ["router"]
