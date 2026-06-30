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


async def has_active_sub(user_id: int) -> bool:
    """Есть ли у пользователя активная подписка (Pro/триал, не истёкшая).
    Используется гейтом: подписчик пускается в само приложение."""
    async with get_connection() as conn:
        return subscription_active(await repo.get_subscription(conn, user_id))


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


async def activate_from_payment(payment_id: str) -> bool:
    """Активировать Pro по платежу ЮKassa. Источник правды — re-GET платежа через
    наш secret (подписи у вебхука нет, поэтому проверяем сами). Идемпотентно:
    upsert подписки + INSERT OR IGNORE платежа дедупят повторный вебхук."""
    from app.billing import yookassa  # ленивый импорт (httpx)

    payment = await yookassa.get_payment(payment_id)
    status = payment.get("status")
    meta = payment.get("metadata") or {}

    if status == "succeeded" and meta.get("user_id"):
        user_id = int(meta["user_id"])
        plan = get_plan(meta.get("plan"))
        period_days = plan.period_days if plan else 30
        pm = payment.get("payment_method") or {}
        method_id = pm.get("id") if pm.get("saved") else None
        now = _utcnow()
        async with get_connection() as conn:
            sub = await repo.get_subscription(conn, user_id)
            license_key = (sub or {}).get("license_key") or generate_license_key()
            fields: dict[str, Any] = {
                "plan": "pro",
                "billing_cycle": (plan.cycle if plan else "monthly"),
                "status": "active",
                "provider": "yookassa",
                "license_key": license_key,
                "amount": (plan.amount if plan else payment["amount"]["value"]),
                "currency": "RUB",
                "current_period_start": _iso(now),
                "current_period_end": _iso(now + timedelta(days=period_days)),
                "cancel_at_period_end": 0,
            }
            if method_id:
                fields["provider_method_id"] = method_id
            await repo.upsert_subscription(conn, user_id, **fields)
            sub = await repo.get_subscription(conn, user_id)
            await repo.set_payment_status(conn, payment_id, "succeeded")
            await repo.record_payment(
                conn, user_id=user_id, subscription_id=(sub or {}).get("id"),
                provider_payment_id=payment_id, idempotence_key=None,
                kind="initial",
                amount=(plan.amount if plan else payment["amount"]["value"]),
                currency="RUB", status="succeeded",
                period_start=_iso(now),
                period_end=_iso(now + timedelta(days=period_days)),
                description=f"Pro {plan.cycle}" if plan else "Pro",
            )
        return True

    async with get_connection() as conn:
        await repo.set_payment_status(conn, payment_id, status or "unknown")
    return False


async def cancel_subscription(user_id: int) -> None:
    """Отметить подписку «не продлевать» (доступ до конца оплаченного периода)."""
    async with get_connection() as conn:
        await repo.upsert_subscription(conn, user_id, cancel_at_period_end=1)
