"""Лицензии и гейтинг Pro.

Роль инстанса (kv ``billing_role``):
  * ``store``  — этот инстанс продаёт подписку; Pro = владелец (деф.);
  * ``client`` — чужой self-host; Pro зависит от валидной лицензии, проверенной
    на сервере-магазине (статус кэшируется в kv ``license_status``/``license_expires``
    валидатором — срез 8).

Гейтинг инстанс-уровневый: данные и фичи общие на инстанс (single-owner), поэтому
is_pro смотрит на состояние инстанса, а не партиционирует по user_id.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from app.auth.owner import is_owner
from app.billing.plans import FREE_RECALL_DAYS, PRO_FEATURES
from app.storage.db import get_connection
from app.storage.repository import get_kv

# без визуально неоднозначных символов (0/O, 1/I)
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_license_key() -> str:
    """Ключ вида PRSN-XXXX-XXXX-XXXX-XXXX, который клиент вставляет в свой self-host."""
    groups = ["".join(secrets.choice(_KEY_ALPHABET) for _ in range(4)) for _ in range(4)]
    return "PRSN-" + "-".join(groups)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    cleaned = ts.strip().replace("Z", "").replace("+00:00", "")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def subscription_active(sub: dict[str, Any] | None) -> bool:
    """Активна ли Pro-подписка (статус + не истёк период). Использует validate-API и портал."""
    if not sub or sub.get("plan") != "pro":
        return False
    if sub.get("status") not in ("active", "trialing", "past_due"):
        return False
    end = _parse(sub.get("current_period_end"))
    if end is not None and end < datetime.utcnow():
        return False
    return True


async def get_billing_role() -> str:
    async with get_connection() as conn:
        raw = await get_kv(conn, "billing_role")
    return (raw or "store").strip().lower()


async def _local_license_active() -> bool:
    """client-инстанс: результат последней валидации лицензии на сервере-магазине."""
    async with get_connection() as conn:
        status = (await get_kv(conn, "license_status") or "").strip()
        expires = await get_kv(conn, "license_expires")
    if status != "active":
        return False
    end = _parse(expires)
    if end is not None and end < datetime.utcnow():
        return False
    return True


async def is_pro(user_id: int | None) -> bool:
    """Включены ли Pro-фичи на этом инстансе для данного пользователя."""
    role = await get_billing_role()
    if role == "store":
        # магазин: Pro у владельца (он и есть «клиент» собственного инстанса)
        return await is_owner(user_id)
    # client-инстанс: Pro зависит от валидной лицензии (инстанс-уровень)
    return await _local_license_active()


async def plan_of(user_id: int | None) -> str:
    return "pro" if await is_pro(user_id) else "free"


async def has_feature(user_id: int | None, feature: str) -> bool:
    """Не-Pro-фичи доступны всем; Pro-фичи — только при активном Pro."""
    if feature not in PRO_FEATURES:
        return True
    return await is_pro(user_id)


async def recall_window_days(user_id: int | None) -> int | None:
    """Глубина recall: None = без лимита (Pro), иначе последние N дней (free)."""
    return None if await is_pro(user_id) else FREE_RECALL_DAYS
