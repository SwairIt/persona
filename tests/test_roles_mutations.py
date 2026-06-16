"""Тесты управления ролями/статусами (app/auth/roles) + гард последнего owner.

Проверяем безопасную часть «auth_gate + роли»: мутации и гарды. Сам auth-gate
(ролевые тиры доступа) НЕ трогаем — он остаётся owner-gate (см. MVP_TASKS).
"""

from __future__ import annotations

import aiosqlite
import pytest

from app.auth.roles import (
    delete_user,
    get_role,
    owner_count,
    set_role,
    set_status,
)


async def _add_user(db: aiosqlite.Connection, uid: int, email: str,
                    role: str = "member", status: str = "active") -> None:
    await db.execute(
        "INSERT INTO users(id,email,password_hash,role,status) VALUES(?,?,?,?,?)",
        (uid, email, "x", role, status),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_owner_count(db: aiosqlite.Connection) -> None:
    await _add_user(db, 1, "o@x.c", role="owner")
    await _add_user(db, 2, "m@x.c", role="member")
    assert await owner_count() == 1


@pytest.mark.asyncio
async def test_cannot_demote_last_owner(db: aiosqlite.Connection) -> None:
    await _add_user(db, 1, "o@x.c", role="owner")
    assert await set_role(1, "member") is False
    assert await get_role(1) == "owner"  # роль не изменилась


@pytest.mark.asyncio
async def test_can_demote_when_two_owners(db: aiosqlite.Connection) -> None:
    await _add_user(db, 1, "o1@x.c", role="owner")
    await _add_user(db, 2, "o2@x.c", role="owner")
    assert await set_role(2, "admin") is True
    assert await get_role(2) == "admin"
    assert await owner_count() == 1


@pytest.mark.asyncio
async def test_cannot_suspend_or_delete_last_owner(db: aiosqlite.Connection) -> None:
    await _add_user(db, 1, "o@x.c", role="owner")
    assert await set_status(1, "suspended") is False
    assert await delete_user(1) is False


@pytest.mark.asyncio
async def test_suspend_member_revokes_sessions(db: aiosqlite.Connection) -> None:
    await _add_user(db, 1, "o@x.c", role="owner")
    await _add_user(db, 2, "m@x.c", role="member")
    await db.execute(
        "INSERT INTO auth_session(token,user_id,expires_at) VALUES(?,?,datetime('now','+1 day'))",
        ("tok-2", 2),
    )
    await db.commit()
    assert await set_status(2, "suspended") is True
    cur = await db.execute("SELECT revoked_at FROM auth_session WHERE user_id=2")
    row = await cur.fetchone()
    assert row["revoked_at"] is not None  # сессия отозвана


@pytest.mark.asyncio
async def test_approve_and_role_change_member(db: aiosqlite.Connection) -> None:
    await _add_user(db, 1, "o@x.c", role="owner")
    await _add_user(db, 2, "p@x.c", role="member", status="pending")
    assert await set_status(2, "active") is True
    assert await set_role(2, "admin") is True
    assert await get_role(2) == "admin"


@pytest.mark.asyncio
async def test_invalid_role_status_rejected(db: aiosqlite.Connection) -> None:
    await _add_user(db, 1, "o@x.c", role="owner")
    await _add_user(db, 2, "m@x.c", role="member")
    assert await set_role(2, "superadmin") is False
    assert await set_status(2, "frozen") is False
    assert await get_role(2) == "member"
