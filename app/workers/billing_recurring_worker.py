"""Автопродление Pro-подписок ЮKassa (рекуррент).

Раз в час сканирует подписки с истёкшим оплаченным периодом и сохранённой
картой (provider_method_id), списывает по сохранённому способу через ЮKassa и
продлевает период. При неуспехе — статус ``past_due`` (доступ держится до конца
текущего периода логикой subscription_active; дальнейший dunning — отдельным шагом).

Триал/ручной грант не трогаем: у них нет provider_method_id, так что
``due_for_renewal`` их не возвращает — триал просто истекает, и юзер платит сам.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from app.billing import config as billing_config
from app.billing import repo
from app.billing.plans import PRO_MONTHLY, PRO_YEARLY
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.workers._bases import BackfillRunner

log = get_logger("persona.billing.recurring")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _plan_for(cycle: str | None):
    return PRO_YEARLY if (cycle or "").strip() == "yearly" else PRO_MONTHLY


async def _list_due() -> list[dict[str, Any]]:
    """Активные подписки с истёкшим периодом и сохранённой картой. Если ЮKassa
    не настроена — ничего (списывать нечем)."""
    if not billing_config.is_configured():
        return []
    async with get_connection() as conn:
        return await repo.due_for_renewal(conn, _iso(_utcnow()))


async def _charge_one(sub: dict[str, Any]) -> Any:
    from app.billing import yookassa  # ленивый импорт (httpx)

    user_id = int(sub["user_id"])
    cycle = sub.get("billing_cycle")
    plan = _plan_for(cycle)
    # Идемпотентность: ключ привязан к user+конец-периода — повторный тик не спишет дважды.
    idem = f"renew_{user_id}_{sub.get('current_period_end')}"
    try:
        payment = await yookassa.charge_saved(
            amount=plan.amount,
            currency=plan.currency,
            description=f"Persona Pro — продление ({plan.cycle})",
            payment_method_id=str(sub["provider_method_id"]),
            metadata={"user_id": str(user_id), "plan": plan.id, "kind": "recurring"},
            idempotence_key=idem,
        )
    except Exception as exc:  # noqa: BLE001 — сбой списания не должен ронять воркер
        log.warning("recurring.charge_error", user_id=user_id, error=str(exc))
        async with get_connection() as conn:
            await repo.upsert_subscription(conn, user_id, status="past_due")
        return None

    now = _utcnow()
    if payment.get("status") == "succeeded":
        new_end = now + timedelta(days=plan.period_days)
        async with get_connection() as conn:
            await repo.upsert_subscription(
                conn, user_id, status="active",
                current_period_start=_iso(now), current_period_end=_iso(new_end),
            )
            await repo.record_payment(
                conn, user_id=user_id, subscription_id=sub.get("id"),
                provider_payment_id=payment.get("id"), idempotence_key=idem,
                kind="recurring", amount=plan.amount, currency=plan.currency,
                status="succeeded", period_start=_iso(now), period_end=_iso(new_end),
                description=f"Pro продление ({plan.cycle})",
            )
        log.info("recurring.renewed", user_id=user_id, until=_iso(new_end))
        return True

    # Списание не прошло (карта/банк) → past_due.
    async with get_connection() as conn:
        await repo.upsert_subscription(conn, user_id, status="past_due")
    log.warning("recurring.declined", user_id=user_id, status=payment.get("status"))
    return None


_runner = BackfillRunner(
    name="billing-recurring",
    poll_seconds=3600,
    list_missing=_list_due,
    build_one=_charge_one,
)


async def run_billing_recurring_worker(stop_event: asyncio.Event | None = None) -> None:
    await _runner.run(stop_event)


__all__ = ["run_billing_recurring_worker"]
