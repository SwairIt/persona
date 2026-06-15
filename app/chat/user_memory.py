"""Личная память ассистента — курируемые факты о пользователе.

Отдельный слой от неявного keyword-recall (`recall_relevant`): здесь живут
явные факты, которые пользователь (или ИИ через /remember) сохранил, чтобы
ассистент «помнил кто ты» между чатами. Подмешивается в системный промпт
(закреплённые + недавние) и доступен инструменту `query_memory`.

Таблица: ``user_memory`` (миграция 180). Без внешних зависимостей.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.user_memory")

_KINDS = {"fact", "preference", "person", "project", "reminder", "other"}
_MAX_LEN = 600


async def add_memory(
    user_id: int,
    text: str,
    kind: str = "fact",
    source_session_id: int | None = None,
    pinned: bool = False,
) -> int | None:
    """Сохранить факт. Дедуп: точное совпадение текста для пользователя не дублируем."""
    text = " ".join((text or "").split())[:_MAX_LEN]
    if not text:
        return None
    if kind not in _KINDS:
        kind = "fact"
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM user_memory WHERE user_id = ? AND lower(text) = lower(?) LIMIT 1",
            (user_id, text),
        )
        existing = await cur.fetchone()
        if existing:
            if pinned:
                await conn.execute(
                    "UPDATE user_memory SET pinned = 1, updated_at = datetime('now') WHERE id = ?",
                    (existing["id"],),
                )
                await conn.commit()
            return int(existing["id"])
        cur = await conn.execute(
            "INSERT INTO user_memory(user_id, kind, text, pinned, source_session_id) "
            "VALUES(?,?,?,?,?)",
            (user_id, kind, text, 1 if pinned else 0, source_session_id),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def list_memory(user_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """Все факты пользователя: закреплённые сверху, потом новые."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, kind, text, pinned, source_session_id, created_at "
            "FROM user_memory WHERE user_id = ? "
            "ORDER BY pinned DESC, id DESC LIMIT ?",
            (user_id, max(1, min(1000, int(limit)))),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": int(r["id"]),
            "kind": str(r["kind"]),
            "text": str(r["text"]),
            "pinned": bool(r["pinned"]),
            "created_at": str(r["created_at"]),
        }
        for r in rows
    ]


async def delete_memory(user_id: int, mem_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute(
            "DELETE FROM user_memory WHERE id = ? AND user_id = ?", (mem_id, user_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def forget(user_id: int, query: str) -> int:
    """Забыть по id (если число) или по подстроке текста. → число удалённых.

    Подстрочный матч делаем в Python (casefold), т.к. SQLite lower()/NOCASE
    работают только с ASCII — кириллицу не приводят к нижнему регистру.
    """
    query = (query or "").strip()
    if not query:
        return 0
    if query.isdigit():
        return 1 if await delete_memory(user_id, int(query)) else 0
    qf = query.casefold()
    rows = await list_memory(user_id, limit=1000)
    ids = [r["id"] for r in rows if qf in r["text"].casefold()]
    if not ids:
        return 0
    async with get_connection() as conn:
        await conn.executemany(
            "DELETE FROM user_memory WHERE id = ? AND user_id = ?",
            [(i, user_id) for i in ids],
        )
        await conn.commit()
    return len(ids)


async def search_memory(user_id: int, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Поиск по фактам (для query_memory). casefold в Python — корректно для кириллицы."""
    q = (query or "").strip().casefold()
    rows = await list_memory(user_id, limit=500)
    if q:
        rows = [r for r in rows if q in r["text"].casefold()]
    return [
        {"id": r["id"], "kind": r["kind"], "text": r["text"], "pinned": r["pinned"]}
        for r in rows[: max(1, min(50, int(limit)))]
    ]


async def build_memory_block(user_id: int, max_items: int = 14) -> str:
    """Блок для системного промпта: закреплённые + недавние факты о пользователе."""
    items = await list_memory(user_id, limit=max_items)
    if not items:
        return ""
    lines = ["── Что я помню о тебе (личная память) ──"]
    for it in items:
        mark = "📌 " if it["pinned"] else "• "
        lines.append(f"{mark}{it['text']}")
    return "\n".join(lines)
