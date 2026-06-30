"""Тесты активации подписки по платежу ЮKassa (вебхук) + отмена.

Мокаем app.billing.yookassa.get_payment (re-GET — авторитетная проверка), чтобы
проверить, что service.activate_from_payment активирует Pro, идемпотентен и не
активирует неуспешный платёж. Без сети.
"""

from __future__ import annotations

import pytest

from app.billing import repo, service, yookassa
from app.storage.db import get_connection


async def _add_user(db, email: str) -> int:
    cur = await db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, "x")
    )
    await db.commit()
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_activate_from_payment_activates_pro(db, monkeypatch):
    uid = await _add_user(db, "buyer@example.io")

    async def fake_get_payment(pid: str):
        return {
            "id": pid,
            "status": "succeeded",
            "amount": {"value": "690.00", "currency": "RUB"},
            "metadata": {"user_id": str(uid), "plan": "pro_monthly"},
            "payment_method": {"id": "pm_123", "saved": True},
        }

    monkeypatch.setattr(yookassa, "get_payment", fake_get_payment)

    ok = await service.activate_from_payment("pay_1")
    assert ok is True
    async with get_connection() as conn:
        sub = await repo.get_subscription(conn, uid)
    assert sub is not None
    assert sub["status"] == "active"
    assert sub["plan"] == "pro"
    assert sub["provider_method_id"] == "pm_123"  # карта сохранена для рекуррента
    assert sub["license_key"]
    # идемпотентность: повторный вебхук не ломает и не дублит
    assert await service.activate_from_payment("pay_1") is True
    async with get_connection() as conn:
        pays = await repo.list_payments(conn, uid)
    assert len([p for p in pays if p["provider_payment_id"] == "pay_1"]) == 1


@pytest.mark.asyncio
async def test_activate_skips_unsucceeded(db, monkeypatch):
    uid = await _add_user(db, "nope@example.io")

    async def fake_get_payment(pid: str):
        return {"id": pid, "status": "canceled", "metadata": {"user_id": str(uid)}}

    monkeypatch.setattr(yookassa, "get_payment", fake_get_payment)
    assert await service.activate_from_payment("pay_x") is False
    async with get_connection() as conn:
        sub = await repo.get_subscription(conn, uid)
    # подписка не активирована
    assert sub is None or sub.get("status") != "active"


@pytest.mark.asyncio
async def test_cancel_subscription(db):
    uid = await _add_user(db, "cancel@example.io")
    await service.grant_pro(uid, 30)
    await service.cancel_subscription(uid)
    async with get_connection() as conn:
        sub = await repo.get_subscription(conn, uid)
    assert sub["cancel_at_period_end"] == 1
