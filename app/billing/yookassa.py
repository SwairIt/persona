"""Клиент ЮKassa (YooKassa API v3) на httpx — без тяжёлого SDK.

Важно для САМОЗАНЯТОГО: ЮKassa с 29.12.2025 прекратила авто-чеки для самозанятых.
Поэтому объект `receipt` (54-ФЗ фискализация) НЕ отправляем — чек НПД владелец
выбивает сам в «Мой налог»/через ФНС-НПД API. Карту списываем, чек — отдельно.

Подписок/планов в ЮKassa нет: рекуррент = наш планировщик дёргает charge_saved()
по сохранённому payment_method_id.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from app.billing.config import get_credentials
from app.logging_setup import get_logger

log = get_logger("persona.billing.yookassa")

_BASE = "https://api.yookassa.ru/v3"

# Официальные IP-диапазоны уведомлений ЮKassa — проверка подлинности вебхуков
# (подписи у ЮKassa нет; верификация = IP-allowlist + повторный GET платежа).
WEBHOOK_IP_RANGES: tuple[str, ...] = (
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "77.75.154.128/25",
    "2a02:5180::/32",
)


class YooKassaError(RuntimeError):
    """Ошибка вызова API ЮKassa (или отсутствующая конфигурация)."""


def _auth() -> tuple[str, str]:
    creds = get_credentials()
    if creds is None:
        raise YooKassaError("ЮKassa не настроена (нет shop_id/secret_key)")
    return (creds.shop_id, creds.secret_key)


async def create_payment(
    *,
    amount: str,
    currency: str,
    description: str,
    return_url: str,
    metadata: dict[str, Any],
    save_payment_method: bool = False,
    idempotence_key: str | None = None,
) -> dict[str, Any]:
    """Первый платёж с redirect-подтверждением. save_payment_method=True привяжет
    карту для будущих рекуррентов (в ответе придёт payment_method.id при saved=true)."""
    body: dict[str, Any] = {
        "amount": {"value": amount, "currency": currency},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": metadata,
    }
    if save_payment_method:
        body["save_payment_method"] = True
    return await _post("/payments", body, idempotence_key or str(uuid.uuid4()))


async def charge_saved(
    *,
    amount: str,
    currency: str,
    description: str,
    payment_method_id: str,
    metadata: dict[str, Any],
    idempotence_key: str,
) -> dict[str, Any]:
    """Рекуррентное списание по сохранённой карте — без участия пользователя.
    idempotence_key детерминируй из (user_id+период), чтобы дабл-ран не списал дважды."""
    body: dict[str, Any] = {
        "amount": {"value": amount, "currency": currency},
        "capture": True,
        "payment_method_id": payment_method_id,
        "description": description[:128],
        "metadata": metadata,
    }
    return await _post("/payments", body, idempotence_key)


async def get_payment(payment_id: str) -> dict[str, Any]:
    """Авторитетный статус платежа (источник правды для вебхука)."""
    async with httpx.AsyncClient(auth=_auth(), timeout=30.0) as client:
        resp = await client.get(f"{_BASE}/payments/{payment_id}")
    if resp.status_code >= 400:
        raise YooKassaError(f"GET payment {payment_id}: {resp.status_code} {resp.text[:200]}")
    return resp.json()


async def _post(path: str, body: dict[str, Any], idempotence_key: str) -> dict[str, Any]:
    headers = {"Idempotence-Key": idempotence_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(auth=_auth(), timeout=30.0) as client:
        resp = await client.post(f"{_BASE}{path}", json=body, headers=headers)
    if resp.status_code >= 400:
        log.warning("yookassa.error", path=path, status=resp.status_code, body=resp.text[:300])
        raise YooKassaError(f"POST {path}: {resp.status_code} {resp.text[:200]}")
    return resp.json()
