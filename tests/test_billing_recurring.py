"""Тесты рекуррент-воркера автопродления подписок (мок ЮKassa charge_saved)."""

from __future__ import annotations

import pytest

from app.billing import repo, yookassa
from app.storage.db import get_connection
from app.workers import billing_recurring_worker as wkr


async def _add_user(db, email: str) -> int:
    cur = await db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, "x")
    )
    await db.commit()
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_list_due_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(wkr.billing_config, "is_configured", lambda: False)
    assert await wkr._list_due() == []


@pytest.mark.asyncio
async def test_charge_one_renews_on_success(db, monkeypatch):
    uid = await _add_user(db, "renew@example.io")
    async with get_connection() as conn:
        await repo.upsert_subscription(
            conn, uid, plan="pro", billing_cycle="monthly", status="active",
            provider="yookassa", provider_method_id="pm_1", license_key="PRSN-X",
            amount="690.00", currency="RUB",
            current_period_start="2026-01-01T00:00:00",
            current_period_end="2026-01-31T00:00:00", cancel_at_period_end=0,
        )
        sub = await repo.get_subscription(conn, uid)

    async def fake_charge(**kwargs):
        return {"id": "rp_1", "status": "succeeded"}

    monkeypatch.setattr(yookassa, "charge_saved", fake_charge)
    assert await wkr._charge_one(sub) is True
    async with get_connection() as conn:
        sub2 = await repo.get_subscription(conn, uid)
        pays = await repo.list_payments(conn, uid)
    assert sub2["status"] == "active"
    assert sub2["current_period_end"] > "2026-02"  # период продлён в будущее
    assert any(p["kind"] == "recurring" and p["status"] == "succeeded" for p in pays)


@pytest.mark.asyncio
async def test_charge_one_declines_to_past_due(db, monkeypatch):
    uid = await _add_user(db, "decline@example.io")
    async with get_connection() as conn:
        await repo.upsert_subscription(
            conn, uid, plan="pro", billing_cycle="monthly", status="active",
            provider="yookassa", provider_method_id="pm_2",
            current_period_end="2026-01-31T00:00:00", cancel_at_period_end=0,
        )
        sub = await repo.get_subscription(conn, uid)

    async def fake_charge(**kwargs):
        return {"id": "rp_2", "status": "canceled"}

    monkeypatch.setattr(yookassa, "charge_saved", fake_charge)
    assert await wkr._charge_one(sub) is None
    async with get_connection() as conn:
        sub2 = await repo.get_subscription(conn, uid)
    assert sub2["status"] == "past_due"
