"""Бизнес-логика биллинга: триалы, ручные гранты, сводка для кабинета, старт оплаты.

Тонкие роуты дёргают этот слой — так логику легко покрыть тестами без HTTP.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.billing import repo
from app.billing.licensing import _parse, generate_license_key, subscription_active
from app.billing.plans import get_plan
from app.storage.db import get_connection

TRIAL_DAYS = 3


def _utcnow() -> datetime:
    # naive-UTC, как хранится в БД (datetime('now') / isoformat без tz)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


async def ensure_trial(user_id: int) -> None:
    """Выдать новому пользователю 3-дневный Pro-триал с лицензией. Если подписка
    уже есть (триал/грант/оплата) — ничего не делаем."""
    async with get_connection() as conn:
        if await repo.get_subscription(conn, user_id) is not None:
            return
        now = _utcnow()
        await repo.upsert_subscription(
            conn, user_id,
            plan="pro", billing_cycle="trial", status="trialing",
            provider="trial", license_key=generate_license_key(),
            amount="0.00", currency="RUB",
            current_period_start=_iso(now),
            current_period_end=_iso(now + timedelta(days=TRIAL_DAYS)),
            cancel_at_period_end=1,
        )


async def grant_pro(user_id: int, days: int) -> str:
    """Ручной грант Pro на ``days`` дней (для своих/тестовых аккаунтов). Возвращает ключ."""
    async with get_connection() as conn:
        existing = await repo.get_subscription(conn, user_id)
        key = (existing or {}).get("license_key") or generate_license_key()
        now = _utcnow()
        await repo.upsert_subscription(
            conn, user_id,
            plan="pro", billing_cycle="yearly", status="active",
            provider="manual", license_key=key, amount="0.00", currency="RUB",
            current_period_start=_iso(now),
            current_period_end=_iso(now + timedelta(days=days)),
            cancel_at_period_end=1,
        )
    return key


def _days_left(sub: dict[str, Any] | None) -> int:
    end = _parse((sub or {}).get("current_period_end"))
    if end is None:
        return 0
    return max(0, math.ceil((end - _utcnow()).total_seconds() / 86400))


async def summary(user_id: int) -> dict[str, Any]:
    """Сводка подписки для кабинета /billing."""
    async with get_connection() as conn:
        sub = await repo.get_subscription(conn, user_id)
    active = subscription_active(sub)
    return {
        "has_sub": sub is not None,
        "plan": (sub or {}).get("plan", "free"),
        "status": (sub or {}).get("status", "none"),
        "active": active,
        "is_trial": bool(sub and sub.get("status") == "trialing"),
        "days_left": _days_left(sub) if active else 0,
        "license_key": (sub or {}).get("license_key"),
        "period_end": (sub or {}).get("current_period_end"),
    }


async def start_checkout(user_id: int, plan_id: str, return_url: str) -> str:
    """Создать платёж ЮKassa (с привязкой карты для рекуррента), записать pending
    payment, вернуть confirmation_url для редиректа. Требует настроенной ЮKassa."""
    plan = get_plan(plan_id)
    if plan is None:
        raise ValueError(f"unknown plan: {plan_id}")
    from app.billing import yookassa  # ленивый импорт (httpx)

    payment = await yookassa.create_payment(
        amount=plan.amount,
        currency=plan.currency,
        description=f"Persona Pro — {plan.cycle}",
        return_url=return_url,
        metadata={"user_id": str(user_id), "plan": plan_id},
        save_payment_method=True,
    )
    async with get_connection() as conn:
        sub = await repo.get_subscription(conn, user_id)
        await repo.record_payment(
            conn, user_id=user_id, subscription_id=(sub or {}).get("id"),
            provider_payment_id=payment.get("id"), idempotence_key=None,
            kind="initial", amount=plan.amount, currency=plan.currency,
            status="pending", description=f"Pro {plan.cycle}",
        )
    return payment.get("confirmation", {}).get("confirmation_url", return_url)
