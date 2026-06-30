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


# Допустимые типы артефактов (мягкая нормализация — мусор не пишем).
_ARTIFACT_TYPES = ("screenshot", "image", "file", "pdf", "html", "text")


def _safe_rel(path_in_workspace: str | None) -> str | None:
    """Нормализовать относительный путь внутри воркспейса (безопасность).

    Артефакт отдаётся наружу как ``/workspace/file/{path}``. Сюда пишем ТОЛЬКО
    относительный путь без выхода вверх (``..``) и без ведущего слэша/диска —
    иначе превратится в обход каталога. Кривой путь → None (артефакт не пишем).
    """
    if not path_in_workspace:
        return None
    rel = str(path_in_workspace).replace("\\", "/").strip().lstrip("/")
    if not rel or ".." in rel.split("/") or ":" in rel:
        return None
    return rel


async def add_artifact(
    exec_id: int | None,
    type: str,
    mime_type: str | None,
    path_in_workspace: str | None,
) -> int | None:
    """Привязать артефакт (скрин/файл) к строке журнала → id (или None при сбое).

    Best-effort: любая ошибка (нет таблицы/exec_id/кривой путь) → тихий None,
    запись артефакта НИКОГДА не должна ломать вызов инструмента.
    """
    if exec_id is None:
        return None
    rel = _safe_rel(path_in_workspace)
    if rel is None:
        return None
    art_type = str(type or "file")
    if art_type not in _ARTIFACT_TYPES:
        art_type = "file"
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO tool_artifact(exec_id, type, mime_type, path_in_workspace) "
                "VALUES(?,?,?,?)",
                (int(exec_id), art_type, (str(mime_type) if mime_type else None), rel),
            )
            await conn.commit()
            return int(cur.lastrowid)
    except Exception as exc:  # noqa: BLE001 — best-effort, не ломаем инструмент
        log.debug("activity.add_artifact_failed", error=str(exc))
        return None


def add_artifact_sync(
    exec_id: int | None,
    type: str,
    mime_type: str | None,
    path_in_workspace: str | None,
    db_path: str | None = None,
) -> int | None:
    """Синхронный вариант add_artifact — для браузер-воркера (отдельный процесс).

    Воркер (``python -m app.browse.agent.worker``) — это синхронный subprocess
    без event-loop, поэтому пишет артефакт обычным ``sqlite3``. Best-effort:
    любая ошибка → None, скриншот и SSE продолжают работать как раньше.
    """
    if exec_id is None:
        return None
    rel = _safe_rel(path_in_workspace)
    if rel is None:
        return None
    art_type = str(type or "file")
    if art_type not in _ARTIFACT_TYPES:
        art_type = "file"
    try:
        import sqlite3  # noqa: PLC0415 — лениво, только в воркере

        if db_path is None:
            from app.settings import get_settings  # noqa: PLC0415

            db_path = str(get_settings().db_path)
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.execute(
                "INSERT INTO tool_artifact(exec_id, type, mime_type, path_in_workspace) "
                "VALUES(?,?,?,?)",
                (int(exec_id), art_type, (str(mime_type) if mime_type else None), rel),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — best-effort, воркер не падает
        log.debug("activity.add_artifact_sync_failed", error=str(exc))
        return None


async def list_artifacts(exec_id: int) -> list[dict[str, Any]]:
    """Артефакты одной строки журнала (для replay/детализации)."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT type, mime_type, path_in_workspace, created_at "
                "FROM tool_artifact WHERE exec_id = ? ORDER BY id ASC",
                (int(exec_id),),
            )
            rows = await cur.fetchall()
        return [_artifact_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("activity.list_artifacts_failed", error=str(exc))
        return []


def _artifact_to_dict(r: Any) -> dict[str, Any]:
    rel = str(r["path_in_workspace"] or "")
    return {
        "type": r["type"],
        "mime_type": r["mime_type"],
        "path": rel,
        "url": (f"/workspace/file/{rel}" if rel else None),
        "created_at": r["created_at"],
    }


async def _attach_artifacts(
    conn: Any, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Подмешать артефакты к строкам активности одним запросом (best-effort).

    Таблица tool_artifact могла не примениться (старая БД) — тогда тихо
    возвращаем строки без артефактов, лента активности продолжает работать.
    """
    if not items:
        return items
    ids = [it["id"] for it in items]
    placeholders = ",".join("?" for _ in ids)
    try:
        cur = await conn.execute(
            "SELECT exec_id, type, mime_type, path_in_workspace, created_at "
            f"FROM tool_artifact WHERE exec_id IN ({placeholders}) ORDER BY id ASC",
            ids,
        )
        rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — нет таблицы и т.п.
        log.debug("activity.attach_artifacts_failed", error=str(exc))
        return items
    by_exec: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_exec.setdefault(int(r["exec_id"]), []).append(_artifact_to_dict(r))
    for it in items:
        arts = by_exec.get(it["id"], [])
        it["artifacts"] = arts
        # удобный шорткат для UI: первый артефакт-картинка (превью)
        it["artifact"] = next(
            (a["url"] for a in arts if a.get("url")), None
        )
    return items


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
        "artifacts": [],
        "artifact": None,
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
        items = [_row_to_dict(r) for r in rows]
        return await _attach_artifacts(conn, items)


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
        items = [_row_to_dict(r) for r in rows]
        return await _attach_artifacts(conn, items)
