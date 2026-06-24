"""Юнит-тесты фундамента биллинга (срез 1): миграция, тарифы, гейтинг, репозиторий, конфиг."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.billing import config, licensing, repo
from app.billing.plans import PLANS, PRO_MONTHLY, PRO_YEARLY
from app.storage.repository import set_kv


@pytest.fixture(autouse=True)
def _reset_owner_cache():
    """owner-id кэшируется на 60с в модуле — сбрасываем между тестами (свежая tmp-БД)."""
    from app.auth import owner

    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    yield


async def _add_user(db, email: str) -> int:
    cur = await db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, "x")
    )
    await db.commit()
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_migration_creates_tables(db):
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('subscription','payment')"
    )
    names = {r["name"] for r in await cur.fetchall()}
    assert names == {"subscription", "payment"}


@pytest.mark.asyncio
async def test_owner_is_pro_on_store(db):
    uid = await _add_user(db, "owner@example.io")
    await set_kv(db, "owner_user_id", str(uid))
    # billing_role не задан → 'store'; владелец магазина = Pro
    assert await licensing.is_pro(uid) is True
    assert await licensing.plan_of(uid) == "pro"
    assert await licensing.recall_window_days(uid) is None


@pytest.mark.asyncio
async def test_non_owner_is_free(db):
    owner_id = await _add_user(db, "owner@example.io")
    member = await _add_user(db, "member@example.io")
    await set_kv(db, "owner_user_id", str(owner_id))
    assert await licensing.is_pro(member) is False
    assert await licensing.plan_of(member) == "free"
    assert await licensing.has_feature(member, "nightly_reflection") is False
    assert await licensing.has_feature(member, "some_basic_thing") is True  # не Pro-фича
    assert await licensing.recall_window_days(member) == 30


@pytest.mark.asyncio
async def test_client_instance_uses_license(db):
    uid = await _add_user(db, "client-owner@example.io")
    await set_kv(db, "owner_user_id", str(uid))
    await set_kv(db, "billing_role", "client")  # чужой self-host
    # без валидной лицензии даже владелец client-инстанса не Pro
    assert await licensing.is_pro(uid) is False
    future = (datetime.utcnow() + timedelta(days=3)).isoformat()
    await set_kv(db, "license_status", "active")
    await set_kv(db, "license_expires", future)
    assert await licensing.is_pro(uid) is True
    # истёкшая лицензия → не Pro
    await set_kv(db, "license_expires", (datetime.utcnow() - timedelta(days=1)).isoformat())
    assert await licensing.is_pro(uid) is False


def test_subscription_active_helper():
    future = (datetime.utcnow() + timedelta(days=5)).isoformat()
    past = (datetime.utcnow() - timedelta(days=1)).isoformat()
    assert licensing.subscription_active({"plan": "pro", "status": "active", "current_period_end": future}) is True
    assert licensing.subscription_active({"plan": "pro", "status": "active", "current_period_end": past}) is False
    assert licensing.subscription_active({"plan": "pro", "status": "canceled", "current_period_end": future}) is False
    assert licensing.subscription_active({"plan": "free", "status": "active", "current_period_end": future}) is False
    assert licensing.subscription_active(None) is False


@pytest.mark.asyncio
async def test_repo_roundtrip_and_dedup(db):
    uid = await _add_user(db, "buyer@example.io")
    await repo.upsert_subscription(
        db, uid, plan="pro", billing_cycle="monthly", status="active", license_key="PRSN-TEST-KEY"
    )
    sub = await repo.get_subscription(db, uid)
    assert sub is not None and sub["plan"] == "pro" and sub["license_key"] == "PRSN-TEST-KEY"
    assert (await repo.get_subscription_by_license(db, "PRSN-TEST-KEY"))["user_id"] == uid

    await repo.record_payment(
        db, user_id=uid, subscription_id=sub["id"], provider_payment_id="pay_1",
        idempotence_key="k1", kind="initial", amount="690.00",
    )
    # дубль того же вебхука (provider_payment_id) — INSERT OR IGNORE
    await repo.record_payment(
        db, user_id=uid, subscription_id=sub["id"], provider_payment_id="pay_1",
        idempotence_key="k1", kind="initial", amount="690.00",
    )
    assert len(await repo.list_payments(db, uid)) == 1


def test_plans_prices():
    assert PRO_MONTHLY.amount == "690.00" and PRO_MONTHLY.amount_rub == 690
    assert PRO_YEARLY.amount == "5900.00" and PRO_YEARLY.amount_rub == 5900
    assert set(PLANS) == {"pro_monthly", "pro_yearly"}


def test_license_key_format():
    key = licensing.generate_license_key()
    parts = key.split("-")
    assert key.startswith("PRSN-") and len(parts) == 5 and all(len(p) == 4 for p in parts[1:])
    # ключи уникальны
    assert licensing.generate_license_key() != licensing.generate_license_key()


def test_config_env_and_file(monkeypatch):
    monkeypatch.delenv("PERSONA_YOOKASSA_SHOP_ID", raising=False)
    monkeypatch.delenv("PERSONA_YOOKASSA_SECRET_KEY", raising=False)
    # ни env, ни файла (data_dir = tmp из autouse-фикстуры) → не настроено
    assert config.get_credentials() is None
    assert config.is_configured() is False
    # запись в data-dir файл и чтение обратно
    config.save_credentials("shop123", "secret456", live=False)
    creds = config.get_credentials()
    assert creds is not None and creds.shop_id == "shop123" and creds.secret_key == "secret456"
    assert creds.live is False
    # env перебивает файл
    monkeypatch.setenv("PERSONA_YOOKASSA_SHOP_ID", "envshop")
    monkeypatch.setenv("PERSONA_YOOKASSA_SECRET_KEY", "envsecret")
    assert config.get_credentials().shop_id == "envshop"
