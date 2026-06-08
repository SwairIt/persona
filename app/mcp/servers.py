"""DB-backed MCP server registry."""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.mcp")


async def list_servers() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, description, command, enabled, created_at "
            "FROM mcp_server ORDER BY id ASC"
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(r["id"]),
            "name": str(r["name"]),
            "description": str(r["description"]) if r["description"] is not None else "",
            "command": str(r["command"]),
            "enabled": bool(int(r["enabled"] or 0)),
            "created_at": str(r["created_at"]),
        }
        for r in rows
    ]


async def get_server(server_id: int) -> dict[str, Any] | None:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM mcp_server WHERE id = ?", (server_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "description": str(row["description"]) if row["description"] else "",
        "command": str(row["command"]),
        "enabled": bool(int(row["enabled"] or 0)),
    }


async def upsert_server(
    *,
    name: str,
    description: str | None,
    command: str,
    enabled: bool,
) -> int:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO mcp_server (name, description, command, enabled) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  description = excluded.description, "
            "  command = excluded.command, "
            "  enabled = excluded.enabled",
            (name, description, command, 1 if enabled else 0),
        )
        await conn.commit()
        if cursor.lastrowid:
            return int(cursor.lastrowid)
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT id FROM mcp_server WHERE name = ?", (name,))
        row = await cursor.fetchone()
        return int(row["id"]) if row else 0


async def set_enabled(server_id: int, enabled: bool) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE mcp_server SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, server_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def set_command(server_id: int, command: str) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE mcp_server SET command = ? WHERE id = ?",
            (command, server_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def delete_server(server_id: int) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM mcp_server WHERE id = ?", (server_id,)
        )
        await conn.commit()
        return cursor.rowcount > 0
