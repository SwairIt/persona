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

# Публичный адрес сайта для ссылок в письме (вебхук без request.base_url).
_PUBLIC_BASE = "https://persona.getdoday.ru"


async def _send_payment_receipt_email(
    user_id: int, amount: str, cycle: str, period_end: str
) -> None:
    """Брендированное письмо «Оплата получена» подписчику. Никогда не роняет
    активацию — все ошибки (нет email / SMTP выключен / сеть) глотаются.

    Вызывать ТОЛЬКО при первой успешной активации (не при ретрае вебхука)."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT email FROM users WHERE id = ?", (user_id,)
            )
            row = await cur.fetchone()
        email = (dict(row).get("email") if row else None) or ""
        email = email.strip()
        if not email:
            return

        from app.mail_branding import branded_email_html
        from app.smtp_delivery import send_email

        cycle_human = {"yearly": "год", "monthly": "месяц"}.get(cycle, cycle)
        chat_url = f"{_PUBLIC_BASE}/chat"
        billing_url = f"{_PUBLIC_BASE}/billing"

        text = (
            "Оплата получена — спасибо!\n\n"
            f"Сумма: {amount} ₽ · период: {cycle_human}\n"
            f"Доступ открыт до: {period_end}\n\n"
            f"Перейти в чат: {chat_url}\n"
            f"Кабинет подписки: {billing_url}\n"
        )
        extra = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="margin:2px 0 14px;"><tr><td style="background:#130a2e;'
            'border:1px solid rgba(147,130,255,.25);border-radius:12px;padding:14px 18px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'line-height:1.7;color:#c5bde0;">'
            f"<b style=\"color:#e9e3ff;\">Сумма:</b> {amount} &#8381; · "
            f"<b style=\"color:#e9e3ff;\">период:</b> {cycle_human}<br>"
            f"<b style=\"color:#e9e3ff;\">Доступ открыт до:</b> {period_end}"
            "</div></td></tr></table>"
            '<p style="margin:0 0 4px;font-family:Arial,Helvetica,sans-serif;'
            'font-size:13px;line-height:1.6;color:#9a90c0;">'
            f'Кабинет подписки и ключ лицензии — на странице '
            f'<a href="{billing_url}" style="color:#c4b5fd;">/billing</a>.</p>'
        )
        html = branded_email_html(
            preheader="Оплата получена — Persona Pro активирован.",
            heading="Оплата получена 🎉",
            lead="Спасибо! Подписка Persona Pro активна. Можешь сразу открывать чат "
            "с памятью — всё включено.",
            button_label="Открыть чат",
            button_url=chat_url,
            extra_html=extra,
            footer="Это автоматическое письмо о платеже. Управлять подпиской можно "
            "в кабинете /billing.",
        )
        await send_email(email, "Оплата получена — Persona Pro", text, html)
    except Exception:  # noqa: BLE001 — письмо не должно ломать активацию
        pass


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
        # Дедуп письма: если этот платёж уже был "succeeded" — это повторный
        # вебхук (ЮKassa ретраит), письмо НЕ слать второй раз.
        async with get_connection() as conn:
            prior = await repo.get_payment_by_provider_id(conn, payment_id)
        already_succeeded = bool(prior and prior.get("status") == "succeeded")
        period_end_iso = _iso(now + timedelta(days=period_days))
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
                period_end=period_end_iso,
                description=f"Pro {plan.cycle}" if plan else "Pro",
            )
        if not already_succeeded:
            await _send_payment_receipt_email(
                user_id,
                amount=str(plan.amount if plan else payment["amount"]["value"]),
                cycle=(plan.cycle if plan else "monthly"),
                period_end=period_end_iso,
            )
        return True

    async with get_connection() as conn:
        await repo.set_payment_status(conn, payment_id, status or "unknown")
    return False


async def cancel_subscription(user_id: int) -> None:
    """Отметить подписку «не продлевать» (доступ до конца оплаченного периода)."""
    async with get_connection() as conn:
        await repo.upsert_subscription(conn, user_id, cancel_at_period_end=1)
