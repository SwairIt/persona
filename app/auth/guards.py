"""Role-based FastAPI guards (АДДИТИВНО, defense-in-depth).

``require_role(min_role)`` — фабрика FastAPI-зависимости (``Depends``): резолвит
роль текущего пользователя (с TTL-кэшем по образцу ``app.auth.owner``) и пускает,
если ``role >= min_role`` по иерархии ``viewer < member < admin < owner``. Иначе —
``HTTPException 403``. ``owner`` всегда суперсет (старший ранг), поэтому владелец
проходит любой ролевой гард.

``role_at_least(uid, min_role) -> bool`` — тонкий хелпер для не-зависимого кода
(например, ветвление в middleware).

Резолв роли кэшируется в процессе (60с TTL), чтобы гард не бил в БД на каждый
запрос. Кэш — на пользователя. Сбой БД/кэша при резолве трактуется fail-open к
безопасному базовому уровню (роль ``member``), как и ``app.auth.roles.get_role``,
чтобы ролевой слой никогда не «кирпичил» приложение. Сам отказ доступа (403)
по-прежнему происходит, если резолвленная роль ниже требуемой.
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from fastapi import Depends, HTTPException, status

from app.auth.dependency import current_user_required
from app.auth.roles import ROLE_RANK, get_role
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger

log = get_logger("persona.auth.guards")

# Базовый ранг при неизвестной роли — как _DEFAULT_ROLE в roles.py ('member').
_DEFAULT_RANK = ROLE_RANK["member"]

# TTL-кэш роли по user_id (по образцу app.auth.owner). 60с.
_ROLE_TTL = 60.0
_role_cache: dict[int, tuple[str, float]] = {}


def _rank(role: str) -> int:
    """Ранг роли; неизвестная роль → базовый уровень (member)."""
    return ROLE_RANK.get(role, _DEFAULT_RANK)


async def resolve_role(user_id: int | None) -> str:
    """Роль пользователя с in-process TTL-кэшем.

    Fail-open к 'member' при сбое (унаследовано от ``roles.get_role``).
    ``None`` (нет сессии) → 'viewer' (минимум прав), тоже как ``get_role``.
    """
    if user_id is None:
        return "viewer"
    now = time.monotonic()
    cached = _role_cache.get(user_id)
    if cached is not None and now - cached[1] < _ROLE_TTL:
        return cached[0]
    try:
        role = await get_role(user_id)
    except Exception as exc:  # noqa: BLE001 — никогда не валим запрос на резолве
        log.debug("guards.resolve_failed", user_id=user_id, error=str(exc))
        # Не кэшируем сбой: вернём безопасный базовый уровень, дадим ретрай позже.
        return "member"
    _role_cache[user_id] = (role, now)
    return role


def _invalidate_role_cache(user_id: int | None = None) -> None:
    """Сбросить кэш роли (всё или одного юзера). Для тестов / смены роли."""
    if user_id is None:
        _role_cache.clear()
    else:
        _role_cache.pop(user_id, None)


async def role_at_least(user_id: int | None, min_role: str) -> bool:
    """True, если роль пользователя >= ``min_role`` по иерархии.

    ``owner`` — старший ранг (суперсет): проходит любой порог.
    """
    return _rank(await resolve_role(user_id)) >= _rank(min_role)


def require_role(
    min_role: str,
) -> Callable[[SessionRecord], Awaitable[SessionRecord]]:
    """Фабрика FastAPI-зависимости: пускает, если роль >= ``min_role``.

    Использование::

        @router.get("/admin/thing", dependencies=[Depends(require_role("admin"))])
        async def thing(...): ...

    Сначала отрабатывает ``current_user_required`` (нет сессии → 303 на логин),
    затем сверяется роль. Недостаточная роль → ``HTTPException 403``.
    ``owner`` проходит всегда (старший ранг).
    """
    need = _rank(min_role)

    async def dependency(
        session: SessionRecord = Depends(current_user_required),
    ) -> SessionRecord:
        uid = session.get("user_id")
        if _rank(await resolve_role(uid)) >= need:
            return session
        log.info("guards.denied", user_id=uid, min_role=min_role)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient role",
        )

    return dependency


__all__ = [
    "require_role",
    "role_at_least",
    "resolve_role",
]
