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


def role_rank(role: str | None) -> int:
    """Числовой ранг роли по иерархии viewer<member<admin<owner.

    Неизвестная/пустая роль → 0 (ниже любого валидного уровня), чтобы сравнения
    были безопасны. ``owner`` — старший ранг (суперсет). Чисто read-функция, БД
    не трогает; для сравнения уже резолвленных ролей.
    """
    return ROLE_RANK.get(role or "", 0)


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


async def owner_count() -> int:
    """Сколько аккаунтов с ролью owner. Гард «нельзя снести последнего owner»."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'owner'")
            row = await cur.fetchone()
        return int(row["n"]) if row else 0
    except Exception as exc:  # noqa: BLE001
        log.debug("roles.owner_count_failed", error=str(exc))
        return 0


async def _is_owner_row(user_id: int) -> bool:
    return (await get_role(user_id)) == "owner"


async def set_status(user_id: int, status: str) -> bool:
    """Сменить статус (active/suspended/pending). При suspend — ревок сессий.

    Гард: нельзя suspend последнего owner (иначе можно остаться без доступа).
    """
    if status not in VALID_STATUS:
        return False
    if status != "active" and await _is_owner_row(user_id) and await owner_count() <= 1:
        log.warning("roles.refuse_suspend_last_owner", user_id=user_id)
        return False
    try:
        async with get_connection() as conn:
            await conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
            if status == "suspended":
                # Немедленно выкидываем: ревок всех сессий → verify_session вернёт
                # None → обычный auth-gate отправит на /landing (без правок gate).
                await conn.execute(
                    "UPDATE auth_session SET revoked_at = datetime('now') "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (user_id,),
                )
            await conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("roles.set_status_failed", user_id=user_id, error=str(exc))
        return False


async def set_role(user_id: int, role: str) -> bool:
    """Сменить роль. Гард: нельзя снять роль owner у последнего owner."""
    if role not in VALID_ROLES:
        return False
    if role != "owner" and await _is_owner_row(user_id) and await owner_count() <= 1:
        log.warning("roles.refuse_demote_last_owner", user_id=user_id)
        return False
    try:
        async with get_connection() as conn:
            await conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            await conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("roles.set_role_failed", user_id=user_id, error=str(exc))
        return False


async def delete_user(user_id: int) -> bool:
    """Снести строку ``users`` и всё, что уедет каскадом БД. НЕ полное удаление.

    ⚠️ Для удаления ЧЕЛОВЕКА (из пульта или по его собственному требованию)
    используйте :func:`app.auth.account_delete.delete_own_account`. Здесь —
    один ``DELETE FROM users``, а он полагается только на ``ON DELETE
    CASCADE``: строки ``training_dataset`` с полным текстом пары «вопрос —
    ответ» отвязываются через ``SET NULL`` и ОСТАЮТСЯ на диске, «хвостатые»
    ключи глобального ``kv_settings`` каскад не забирает вовсе, а FTS-зеркало
    сообщений синхронизируется триггерами на ``chat_message``. Инвентаризация
    — в докстринге ``app/auth/account_delete.py``.

    Функция сохранена как узкая проверка каскада схемы (её зовут тесты
    целостности), а не как способ удалить пользователя.

    Гард: нельзя удалить последнего owner.
    """
    if await _is_owner_row(user_id) and await owner_count() <= 1:
        log.warning("roles.refuse_delete_last_owner", user_id=user_id)
        return False
    try:
        async with get_connection() as conn:
            await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("roles.delete_user_failed", user_id=user_id, error=str(exc))
        return False


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


__all__ = [
    "ROLE_RANK", "VALID_ROLES", "VALID_STATUS",
    "role_rank", "get_role", "has_permission", "list_users",
    "owner_count", "set_status", "set_role", "delete_user",
]
