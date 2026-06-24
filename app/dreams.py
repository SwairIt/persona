"""Репозиторий ночной рефлексии («сны» Hermes-style) — таблица ``reflection``.

Procedural-ярус памяти (docs/MEMORY_RESEARCH.md §2.1/§2.3): инсайты ночной
рефлексии (Generative-Agents reflection tree, ``kind='insight'``), REM-дневник
«снов» (Hermes DREAMS.md, ``kind='dream'``) и Reflexion-заметки
(``kind='self_note'``). Подмешивается в системный промпт отдельным блоком
(``build_memory_block`` в ``app/chat/user_memory.py``).

Bi-temporal soft-invalidate (как ``user_memory``): ``valid_until IS NULL`` →
актуальная запись; штамп времени → ушла из выдачи, но осталась в истории.
Таблица создаётся миграцией ``191_reflections.sql``. Без внешних зависимостей.
"""

from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction

log = get_logger("persona.dreams")

_KINDS = {"insight", "dream", "self_note"}
_MAX_LEN = 1200


async def add_reflection(
    user_id: int,
    text: str,
    kind: str = "insight",
    source_message_ids: list[int] | None = None,
    importance: float | None = None,
) -> int | None:
    """Записать рефлексию. Возвращает id или None (пустой текст)."""
    text = " ".join((text or "").split())[:_MAX_LEN]
    if not text:
        return None
    if kind not in _KINDS:
        kind = "insight"
    src = json.dumps(source_message_ids) if source_message_ids else None
    async with write_transaction() as conn:
        cur = await conn.execute(
            "INSERT INTO reflection(user_id, kind, text, source_message_ids, importance) "
            "VALUES(?,?,?,?,?)",
            (user_id, kind, text, src, importance),
        )
        return int(cur.lastrowid)


async def list_active_reflections(
    user_id: int, kinds: list[str] | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Актуальные рефлексии (``valid_until IS NULL``), новые сверху.

    ``kinds`` — фильтр по виду (``insight``/``dream``/``self_note``); пусто/None →
    все виды. Best-effort: на старой БД без таблицы вызывающий ловит исключение.
    """
    wanted = [k for k in (kinds or []) if k in _KINDS]
    where = "user_id = ? AND valid_until IS NULL"
    params: list[Any] = [user_id]
    if wanted:
        where += " AND kind IN (%s)" % ",".join("?" * len(wanted))
        params.extend(wanted)
    params.append(max(1, min(100, int(limit))))
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, kind, text, source_message_ids, importance, created_at "
            f"FROM reflection WHERE {where} ORDER BY id DESC LIMIT ?",
            params,
        )
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        src = r["source_message_ids"]
        try:
            ids = json.loads(src) if src else []
        except (ValueError, TypeError):
            ids = []
        out.append(
            {
                "id": int(r["id"]),
                "kind": str(r["kind"]),
                "text": str(r["text"]),
                "source_message_ids": ids,
                "importance": r["importance"],
                "created_at": str(r["created_at"]),
            }
        )
    return out


async def invalidate_reflection(user_id: int, reflection_id: int) -> bool:
    """Soft-invalidate рефлексии (``valid_until = now``). НЕ удаляет — уходит из
    выдачи, остаётся в истории. → True, если что-то изменили."""
    async with write_transaction() as conn:
        cur = await conn.execute(
            "UPDATE reflection SET valid_until = datetime('now') "
            "WHERE id = ? AND user_id = ? AND valid_until IS NULL",
            (reflection_id, user_id),
        )
        return cur.rowcount > 0


__all__ = ["add_reflection", "invalidate_reflection", "list_active_reflections"]
