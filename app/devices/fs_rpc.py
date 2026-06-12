"""T29 — remote filesystem RPC over the Mac agent.

The AI's file tools enqueue a command here; the Mac agent polls, executes
it on the real Mac filesystem (within the user's allowlisted dirs), and
posts the result back. Nothing the AI creates is stored on the server.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

_DEFAULT_ROOTS = "~/Projects"
_ROOTS_KEY = "mac_fs_roots"
_ENABLED_KEY = "mac_fs_enabled"


async def get_roots() -> list[str]:
    async with get_connection() as conn:
        raw = await get_kv(conn, _ROOTS_KEY)
    raw = raw if (raw and raw.strip()) else _DEFAULT_ROOTS
    return [r.strip() for r in raw.replace(",", "\n").splitlines() if r.strip()]


async def set_roots(roots: list[str]) -> None:
    cleaned = "\n".join(r.strip() for r in roots if r.strip())
    async with get_connection() as conn:
        await set_kv(conn, _ROOTS_KEY, cleaned or _DEFAULT_ROOTS)


async def is_enabled() -> bool:
    async with get_connection() as conn:
        return (await get_kv(conn, _ENABLED_KEY) or "0").strip() == "1"


async def set_enabled(on: bool) -> None:
    async with get_connection() as conn:
        await set_kv(conn, _ENABLED_KEY, "1" if on else "0")


async def online_target_device(user_id: int) -> dict[str, Any] | None:
    """The code-write-target device for this user (the Mac that executes
    file ops). Returns the device row or None."""
    from app.devices import list_devices  # noqa: PLC0415

    for d in await list_devices(user_id):
        if d.get("is_code_write_target"):
            return d
    return None


async def enqueue(
    device_id: int, user_id: int, op: str, path: str, content: str | None = None
) -> int:
    async with get_connection() as conn:
        cur = await conn.execute(
            "INSERT INTO agent_fs_command (device_id, user_id, op, path, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (device_id, user_id, op, path, content),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)


async def wait_result(command_id: int, timeout: float = 30.0) -> dict[str, str]:
    """Poll until the agent completes the command, or time out."""
    waited = 0.0
    step = 0.5
    while waited < timeout:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT status, result FROM agent_fs_command WHERE id = ?",
                (command_id,),
            )
            row = await cur.fetchone()
        if row is not None and str(row["status"]) != "pending":
            return {"status": str(row["status"]), "result": str(row["result"] or "")}
        await asyncio.sleep(step)
        waited += step
    return {
        "status": "timeout",
        "result": "Mac-агент не ответил вовремя (offline или занят).",
    }


async def get_pending(device_id: int, limit: int = 10) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, op, path, content FROM agent_fs_command "
            "WHERE device_id = ? AND status = 'pending' ORDER BY id LIMIT ?",
            (device_id, max(1, min(50, limit))),
        )
        return [dict(r) for r in await cur.fetchall()]


async def submit_result(command_id: int, status: str, result: str) -> None:
    safe_status = status if status in ("done", "error") else "error"
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE agent_fs_command SET status = ?, result = ?, "
            "completed_at = datetime('now') WHERE id = ?",
            (safe_status, result[:2_000_000], command_id),
        )
        await conn.commit()


async def run_remote(user_id: int, op: str, path: str, content: str | None = None) -> str:
    """High-level: enqueue an op for the user's Mac + wait for the result.
    Returns a ``[ok] ...`` / ``[error] ...`` string for the LLM. Returns
    None-like error if no online target device."""
    device = await online_target_device(user_id)
    if device is None:
        return "[error] нет выбранного Mac-устройства (code target) — выбери на /devices"
    cmd_id = await enqueue(int(device["id"]), user_id, op, path, content)
    res = await wait_result(cmd_id)
    if res["status"] == "done":
        return res["result"]
    if res["status"] == "timeout":
        return f"[error] {res['result']}"
    return f"[error] {res['result'] or 'агент вернул ошибку'}"
