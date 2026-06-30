"""CRUD по subscription/payment. Стиль как в app/storage/repository.py:
функции принимают aiosqlite.Connection, параметризованные запросы, явный commit.
"""

from __future__ import annotations

from typing import Any

import aiosqlite


# --------------------------------------------------------------------------- subscription

async def get_subscription(conn: aiosqlite.Connection, user_id: int) -> dict[str, Any] | None:
    cur = await conn.execute("SELECT * FROM subscription WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_subscription_by_license(
    conn: aiosqlite.Connection, license_key: str
) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT * FROM subscription WHERE license_key = ?", (license_key,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_subscription(conn: aiosqlite.Connection, user_id: int, **fields: Any) -> None:
    """Создать строку подписки пользователя или обновить переданные поля."""
    existing = await get_subscription(conn, user_id)
    if existing is None:
        cols = ["user_id", *fields.keys()]
        placeholders = ", ".join("?" for _ in cols)
        await conn.execute(
            f"INSERT INTO subscription ({', '.join(cols)}) VALUES ({placeholders})",
            [user_id, *fields.values()],
        )
    elif fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        await conn.execute(
            f"UPDATE subscription SET {sets}, updated_at = datetime('now') WHERE user_id = ?",
            [*fields.values(), user_id],
        )
    await conn.commit()


async def list_all_subscriptions(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Все подписки + email/план пользователя — для биллинг-админки владельца."""
    cur = await conn.execute(
        """
        SELECT u.id AS user_id, u.email AS email,
               s.plan AS plan, s.status AS status,
               s.current_period_end AS current_period_end,
               s.provider AS provider, s.license_key AS license_key
        FROM subscription s
        JOIN users u ON u.id = s.user_id
        ORDER BY s.current_period_end DESC
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def due_for_renewal(conn: aiosqlite.Connection, now_iso: str) -> list[dict[str, Any]]:
    """Активные Pro-подписки с истёкшим периодом, которые надо продлить списанием."""
    cur = await conn.execute(
        """
        SELECT * FROM subscription
        WHERE status = 'active' AND cancel_at_period_end = 0
          AND provider_method_id IS NOT NULL
          AND current_period_end IS NOT NULL AND current_period_end <= ?
        """,
        (now_iso,),
    )
    return [dict(r) for r in await cur.fetchall()]


# --------------------------------------------------------------------------- payment

async def record_payment(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    subscription_id: int | None,
    provider_payment_id: str | None,
    idempotence_key: str | None,
    kind: str,
    amount: str,
    currency: str = "RUB",
    status: str = "pending",
    period_start: str | None = None,
    period_end: str | None = None,
    description: str | None = None,
) -> None:
    """Записать платёж. Дедуп по provider_payment_id (INSERT OR IGNORE) — один и тот
    же вебхук может прийти несколько раз."""
    await conn.execute(
        """
        INSERT OR IGNORE INTO payment
            (user_id, subscription_id, provider_payment_id, idempotence_key, kind,
             amount, currency, status, period_start, period_end, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, subscription_id, provider_payment_id, idempotence_key, kind,
         amount, currency, status, period_start, period_end, description),
    )
    await conn.commit()


async def get_payment_by_provider_id(
    conn: aiosqlite.Connection, provider_payment_id: str
) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT * FROM payment WHERE provider_payment_id = ?", (provider_payment_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_payment_status(
    conn: aiosqlite.Connection, provider_payment_id: str, status: str
) -> None:
    await conn.execute(
        "UPDATE payment SET status = ?, updated_at = datetime('now') "
        "WHERE provider_payment_id = ?",
        (status, provider_payment_id),
    )
    await conn.commit()


async def list_payments(
    conn: aiosqlite.Connection, user_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM payment WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_unfiscalized(conn: aiosqlite.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """Успешные платежи, по которым самозанятому ещё нужно выбить чек в «Мой налог»."""
    cur = await conn.execute(
        "SELECT * FROM payment WHERE status = 'succeeded' AND fiscalized = 0 "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]
