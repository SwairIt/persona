"""Webhook subscriptions storage."""

from __future__ import annotations

from typing import Any

import aiosqlite


async def create_webhook(
    conn: aiosqlite.Connection,
    *,
    url: str,
    event_type: str,
    secret: str | None = None,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO webhooks (url, event_type, secret) VALUES (?, ?, ?)",
        (url, event_type, secret),
    )
    await conn.commit()
    if cursor.lastrowid is None:
        msg = "INSERT returned no row id"
        raise RuntimeError(msg)
    return int(cursor.lastrowid)


async def list_webhooks(
    conn: aiosqlite.Connection,
    *,
    event_type: str | None = None,
    only_enabled: bool = False,
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if event_type is not None:
        where.append("event_type = ?")
        params.append(event_type)
    if only_enabled:
        where.append("enabled = 1")
    sql = "SELECT * FROM webhooks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def delete_webhook(conn: aiosqlite.Connection, webhook_id: int) -> None:
    await conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
    await conn.commit()


async def toggle_webhook(conn: aiosqlite.Connection, webhook_id: int, enabled: bool) -> None:
    await conn.execute(
        "UPDATE webhooks SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, webhook_id),
    )
    await conn.commit()


async def record_delivery(
    conn: aiosqlite.Connection,
    webhook_id: int,
    *,
    status_code: int,
    error: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE webhooks SET last_delivered_at = datetime('now'), "
        "last_status_code = ?, last_error = ? WHERE id = ?",
        (status_code, error, webhook_id),
    )
    await conn.commit()


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "url": str(row["url"]),
        "event_type": str(row["event_type"]),
        "secret": row["secret"],
        "enabled": bool(row["enabled"]),
        "created_at": str(row["created_at"]),
        "last_delivered_at": row["last_delivered_at"],
        "last_status_code": row["last_status_code"],
        "last_error": row["last_error"],
    }
