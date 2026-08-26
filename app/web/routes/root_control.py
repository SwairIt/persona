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
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.web.routes.owner_view import viewer_is_owner as is_owner
from app.web.templates_engine import templates

router = APIRouter(tags=["root"])
log = get_logger("persona.root")


async def _require_owner(session: SessionRecord) -> int:
    """Владелец — или 403. Резолв FAIL-CLOSED.

    ``is_owner`` здесь — это ``owner_view.viewer_is_owner``: любой сбой резолва
    (занятая БД, недоступный каталог ролей) означает «не владелец» → 403, а не
    500 и не тихий проход. Этот пульт умеет замораживать и удалять аккаунты;
    развилка, у которой ветка по умолчанию — «пусти», здесь недопустима.
    """
    uid = session["user_id"]
    if not await is_owner(uid):
        raise HTTPException(status_code=403, detail="только для владельца")
    return uid


#: Куда разрешено вернуть браузер после мутации аккаунта. Значение приходит из
#: формы, поэтому это ЗАКРЫТЫЙ список, а не «любой относительный путь»: иначе
#: ``next`` превращается в open redirect на owner-поверхности.
_RETURN_TO: frozenset[str] = frozenset({"/root", "/root/people"})


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


@router.post("/root/users/{uid}/{op}", response_model=None)
async def root_user_mutate(
    uid: int,
    op: str,
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
):
    """Управление пользователями (owner-only, re-assert). op: approve|suspend|delete|role.

    Единственная точка мутации аккаунта на owner-поверхности: сюда шлют формы
    и ``/root``, и ``/root/people`` (страница различается полем ``next``).
    Второй набор ручек означал бы два места, где решается «можно ли снести
    этот аккаунт», и расхождение гардов на первой же правке.

    Гарды: ``app/auth/roles`` не даёт suspend/demote последнего owner;
    ``app/auth/account_delete.can_delete`` не даёт удалить владельца вообще.
    ``suspend`` ревокает сессии (мгновенный выход), ``delete`` идёт полным
    каскадом. Всё пишется в audit.log_action — и успех, и отказ.
    """
    from fastapi.responses import RedirectResponse  # noqa: PLC0415

    owner_id = await _require_owner(session)
    from app.auth.roles import set_role, set_status  # noqa: PLC0415

    form = await request.form()
    back = str(form.get("next") or "/root")
    if back not in _RETURN_TO:
        back = "/root"

    ok = False
    detail = op
    try:
        if op == "approve":
            ok = await set_status(uid, "active")
        elif op == "suspend":
            ok = await set_status(uid, "suspended")
        elif op == "delete":
            # Каскад, а не ``DELETE FROM users``. Голый DELETE полагается на
            # ON DELETE CASCADE, а он покрывает не всё: строки training_dataset
            # (полный текст пары «вопрос — ответ») отвязываются через SET NULL и
            # ОСТАЮТСЯ на диске, FTS-зеркало сообщений синхронизируется
            # триггерами на chat_message, а у kv_settings внешних ключей нет
            # вовсе. Инвентаризация — в docstring app/auth/account_delete.py;
            # держать её в двух местах невозможно, поэтому удалятель один.
            from app.auth.account_delete import (  # noqa: PLC0415
                delete_own_account,
            )

            result = await delete_own_account(uid, initiated_by="owner")
            ok = result.ok
            detail = (
                f"cascade rows={result.rows_deleted} kv={result.kv_keys_deleted}"
                if ok
                else f"refused:{result.reason}"
            )
        elif op == "role":
            new_role = str(form.get("role") or "").strip()
            detail = f"role={new_role}"
            ok = await set_role(uid, new_role)
    except Exception as exc:  # noqa: BLE001
        log.warning("root.user_mutate_failed", uid=uid, op=op, error=str(exc))
        ok = False
    try:
        from app.audit import log_action  # noqa: PLC0415

        await log_action(
            action=f"root.user.{op}",
            actor=str(owner_id),
            target=str(uid),
            detail=detail,
            success=ok,
        )
    except Exception:  # noqa: BLE001, S110
        pass
    return RedirectResponse(url=back, status_code=303)


@router.get("/root/logs/recent.json", response_class=JSONResponse)
async def root_logs_recent(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    limit: int = 300,
    level: str = "",
    since: str = "",
) -> JSONResponse:
    """Сводные логи по всем воркерам из durable system_log (F6-06).

    Фильтры: ``level`` (порог уровня), ``since`` (ISO-таймстемп — только новее).
    Durable-таблица хранит только warning+; для уровней ниже (или если БД пуста)
    тихо доливаем из in-memory deque текущего воркера, чтобы пульт не выглядел
    пустым на свежей инсталляции.
    """
    await _require_owner(session)
    from app.log_buffer import buffer_size, get_recent, get_recent_durable  # noqa: PLC0415

    lvl = level or None
    src = "system_log"
    try:
        logs = await get_recent_durable(limit=limit, level=lvl, since=since or None)
    except Exception as exc:  # noqa: BLE001 — durable-чтение best-effort
        log.warning("root.logs_durable_failed", error=str(exc))
        logs = []
    # Fallback на локальный кольцевой буфер: durable хранит лишь warning+,
    # а для info/debug или пустой таблицы показываем хотя бы текущий воркер.
    floor_low = (lvl or "").lower() not in {"warning", "warn", "error", "critical"}
    if not logs and floor_low:
        logs = get_recent(limit=limit, level=lvl)
        src = "memory"
    return JSONResponse({"logs": logs, "buffered": buffer_size(), "source": src})


__all__ = ["router"]
