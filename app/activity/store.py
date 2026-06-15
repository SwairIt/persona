"""Хранилище журнала активности инструментов (таблица ``tool_execution``)."""

from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.activity")

_ARGS_CAP = 4000      # обрезка args_json — не раздуваем БД на больших аргументах
_RESULT_CAP = 8000    # обрезка result_text для replay (UI и так не покажет больше)


def _short(value: str | None, cap: int) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= cap else value[:cap] + "…"


async def start_execution(
    user_id: int,
    tool_name: str,
    args: Any = None,
    *,
    session_id: int | None = None,
    message_id: int | None = None,
    seq: int = 0,
    kind: str = "builtin",
) -> int | None:
    """Записать начало вызова инструмента → id строки (или None при сбое)."""
    try:
        try:
            args_json = json.dumps(args, ensure_ascii=False) if args is not None else None
        except (TypeError, ValueError):
            args_json = str(args)
        async with get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO tool_execution"
                "(user_id, session_id, message_id, seq, kind, tool_name, args_json, status) "
                "VALUES(?,?,?,?,?,?,?, 'running')",
                (
                    user_id,
                    session_id,
                    message_id,
                    int(seq),
                    kind,
                    str(tool_name)[:120],
                    _short(args_json, _ARGS_CAP),
                ),
            )
            await conn.commit()
            return int(cur.lastrowid)
    except Exception as exc:  # noqa: BLE001 — best-effort, не ломаем чат
        log.debug("activity.start_failed", error=str(exc))
        return None


async def finish_execution(
    exec_id: int | None,
    status: str = "done",
    *,
    result_text: str | None = None,
    error_text: str | None = None,
) -> None:
    """Записать завершение вызова + elapsed_ms (по started_at в SQL)."""
    if exec_id is None:
        return
    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE tool_execution SET "
                "  status = ?, result_text = ?, error_text = ?, "
                "  finished_at = datetime('now'), "
                "  elapsed_ms = CAST((julianday('now') - julianday(started_at)) * 86400000 AS INTEGER) "
                "WHERE id = ?",
                (
                    status if status in ("done", "error") else "done",
                    _short(result_text, _RESULT_CAP),
                    _short(error_text, _RESULT_CAP),
                    int(exec_id),
                ),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("activity.finish_failed", error=str(exc))


def _row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "session_id": r["session_id"],
        "seq": int(r["seq"] or 0),
        "kind": str(r["kind"]),
        "tool": str(r["tool_name"]),
        "args": r["args_json"],
        "status": str(r["status"]),
        "result": r["result_text"],
        "error": r["error_text"],
        "started_at": str(r["started_at"]),
        "finished_at": r["finished_at"],
        "elapsed_ms": r["elapsed_ms"],
    }


async def list_session_activity(
    user_id: int, session_id: int, limit: int = 200
) -> list[dict[str, Any]]:
    """Журнал активности одной сессии (для replay в чате), старые → новые."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, session_id, seq, kind, tool_name, args_json, status, "
            "       result_text, error_text, started_at, finished_at, elapsed_ms "
            "FROM tool_execution WHERE user_id = ? AND session_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (user_id, session_id, max(1, min(1000, int(limit)))),
        )
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def list_recent_activity(user_id: int, limit: int = 100) -> list[dict[str, Any]]:
    """Глобальная лента активности пользователя (для страницы /activity), новые → старые."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, session_id, seq, kind, tool_name, args_json, status, "
            "       result_text, error_text, started_at, finished_at, elapsed_ms "
            "FROM tool_execution WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, max(1, min(500, int(limit)))),
        )
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]
