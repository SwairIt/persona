"""Роли пользователей (фундамент) — read/helpers, БЕЗ смены auth_gate.

Колонки ``users.role`` / ``users.status`` появились в миграции 184. Сейчас это
тонкий слой чтения: ``get_role`` / ``has_permission`` / ``list_users`` — для
read-only списка в /root и будущего управления. Текущая защита приватности
по-прежнему держится на owner-gate (``app.auth.owner.is_owner``); ``is_owner``
здесь НЕ переопределяется. Все функции fail-open к безопасному базовому уровню,
чтобы отсутствие/сбой роли никогда не ломал приложение.

Иерархия: viewer < member < admin < owner.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.roles")

ROLE_RANK: dict[str, int] = {"viewer": 1, "member": 2, "admin": 3, "owner": 4}
VALID_ROLES = frozenset(ROLE_RANK)
VALID_STATUS = frozenset({"pending", "active", "suspended"})

_DEFAULT_ROLE = "member"


async def get_role(user_id: int | None) -> str:
    """Роль пользователя. Fail-open к 'member' при сбое/отсутствии колонки."""
    if user_id is None:
        return "viewer"
    try:
        async with get_connection() as conn:
            cur = await conn.execute("SELECT role FROM users WHERE id = ?", (user_id,))
            row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — колонки может не быть на старой БД
        log.debug("roles.get_failed", error=str(exc))
        return _DEFAULT_ROLE
    if row is None:
        return _DEFAULT_ROLE
    role = str(row["role"] or _DEFAULT_ROLE)
    return role if role in VALID_ROLES else _DEFAULT_ROLE


async def has_permission(user_id: int | None, min_role: str = "member") -> bool:
    """True, если роль пользователя >= ``min_role`` по иерархии."""
    need = ROLE_RANK.get(min_role, ROLE_RANK[_DEFAULT_ROLE])
    have = ROLE_RANK.get(await get_role(user_id), 0)
    return have >= need


async def list_users() -> list[dict[str, Any]]:
    """Все пользователи (для read-only списка в /root). Best-effort."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT id, email, role, status, created_at, last_login_at, display_name "
                "FROM users ORDER BY id"
            )
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — старая БД без колонок role/status
        log.debug("roles.list_failed", error=str(exc))
        try:
            async with get_connection() as conn:
                cur = await conn.execute(
                    "SELECT id, email, created_at, last_login_at, display_name FROM users ORDER BY id"
                )
                rows = await cur.fetchall()
            return [
                {**dict(r), "role": "—", "status": "—"} for r in rows
            ]
        except Exception:  # noqa: BLE001
            return []
    return [dict(r) for r in rows]


__all__ = ["ROLE_RANK", "VALID_ROLES", "VALID_STATUS", "get_role", "has_permission", "list_users"]
