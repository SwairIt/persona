# ruff: noqa: RUF002, RUF003
"""Append Q&A pairs to ``training_dataset`` after each chat turn.

Designed to be cheap and forgiving:
  * If the kv flag is off → no-op without error.
  * If insert raises (table missing on a fresh install before the
    migration ran) → log a warning and move on. The chat reply still
    reaches the user.

ЧЕЙ ЭТО ДАТАСЕТ (читать перед правкой)
--------------------------------------
Таблица держит ПОЛНЫЙ текст пары «вопрос человека / ответ модели» плюс
системный промпт и предыдущие реплики — и существует ровно для одного:
чтобы ВЛАДЕЛЕЦ инстанса дообучил модель на СВОЁМ голосе. Регистрация на
инстансе открыта всем (v2.33.1), поэтому переписка участника в этой таблице
— это чужой личный текст, который через ``/admin/dataset/export.jsonl``
уезжает владельцу и дальше в веса модели. Отсюда два независимых рубежа:

1. **Запись** (:func:`record_qa_pair`) — строка появляется, только если
   действующий пользователь резолвится как владелец. Резолв fail-closed
   (``viewer_is_owner``): любая ошибка = «участник» = не пишем.
2. **Чтение** (:func:`stats`, :func:`iter_export_rows`) — выборка всё равно
   фильтруется по ``user_id`` владельца. Если завтра кто-то починит баг и
   случайно снова начнёт писать чужие строки, в экспорт они не попадут.

Оба рубежа нужны именно вдвоём: один — от ошибки в другом.
"""

from __future__ import annotations

import json
from typing import Any

from app.auth.owner import owner_user_ids
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

# Fail-closed резолв роли: сбой резолва = «участник» (app/web/routes/owner_view.py).
from app.web.routes.owner_view import viewer_is_owner as is_owner

log = get_logger("persona.training.collector")

_FLAG_KEY = "training_dataset_enabled"


async def is_enabled() -> bool:
    async with get_connection() as conn:
        raw = await get_kv(conn, _FLAG_KEY)
    return (raw or "1").strip() != "0"


async def set_enabled(enabled: bool) -> None:
    async with get_connection() as conn:
        await set_kv(conn, _FLAG_KEY, "1" if enabled else "0")


async def record_qa_pair(
    *,
    user_id: int | None,
    session_id: int,
    user_message_id: int,
    asst_message_id: int,
    user_text: str,
    assistant_text: str,
    system_prompt: str | None,
    context_turns: list[dict[str, str]] | None,
    image_present: bool,
    provider: str | None,
    model: str | None,
) -> int | None:
    """Persist one (user → assistant) turn. Returns the row id or None
    on no-op / failure.

    ``user_id`` — КТО говорил. Параметр обязательный и без значения по
    умолчанию намеренно: новый вызывающий обязан ответить на этот вопрос, а
    не унаследовать молчаливое «неизвестно». Не владелец → строки не будет.
    """
    # Рубеж 1: чужую переписку в датасет владельца не пишем ВООБЩЕ. Проверка
    # стоит ПЕРЕД флагом сбора: решение «это не владелец» не зависит от того,
    # включён ли сбор, и не должно уметь провалиться сквозь него.
    if not await is_owner(user_id):
        # Логируем сам факт и адресацию — без единого символа текста.
        log.debug(
            "training.record.skipped",
            reason="not_owner",
            user_id=user_id,
            session_id=session_id,
        )
        return None
    if not await is_enabled():
        return None
    if not user_text or not assistant_text:
        return None

    ctx_json = (
        json.dumps(context_turns, ensure_ascii=False)
        if context_turns else None
    )

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO training_dataset "
                "  (user_id, session_id, user_message_id, asst_message_id, "
                "   user_text, assistant_text, system_prompt, "
                "   context_json, image_present, provider, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(user_id) if user_id is not None else None,
                    session_id,
                    user_message_id,
                    asst_message_id,
                    user_text,
                    assistant_text,
                    system_prompt,
                    ctx_json,
                    1 if image_present else 0,
                    provider,
                    model,
                ),
            )
            await conn.commit()
            return int(cursor.lastrowid or 0)
    except Exception as exc:
        log.warning("training.record.failed", error=str(exc))
        return None


async def set_rating(row_id: int, rating: int) -> bool:
    """Update the 👍/👎 thumb on one training row. ``rating`` must be
    -1, 0, or 1."""
    if rating not in (-1, 0, 1):
        raise ValueError("rating must be -1, 0, or 1")
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE training_dataset SET rating = ? WHERE id = ?",
            (rating, row_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def _owner_scope() -> tuple[str, tuple[int, ...]]:
    """SQL-условие «строка принадлежит владельцу» + его параметры.

    Рубеж 2 (см. модульную docstring). Возвращает ``("user_id IN (?, ?)", ids)``
    — или ``("0", ())``, если владелец не резолвится: пустая выдача, а не вся
    таблица. NULL в ``user_id`` (строка до миграции 236 / запись в обход
    :func:`record_qa_pair`) под ``IN`` не подходит НИКОГДА — это и нужно:
    неатрибутируемая строка не считается владельческой.
    """
    ids = sorted(await owner_user_ids())
    if not ids:
        log.warning("training.owner_scope.unresolved")
        return "0", ()
    marks = ", ".join("?" for _ in ids)
    return f"user_id IN ({marks})", tuple(int(i) for i in ids)


async def stats() -> dict[str, Any]:
    """Counts for the /admin/dataset dashboard — ТОЛЬКО строки владельца."""
    scope, scope_params = await _owner_scope()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n, "  # noqa: S608 — в скоуп подставляются только "?"
            "       SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END) AS good, "
            "       SUM(CASE WHEN rating=-1 THEN 1 ELSE 0 END) AS bad, "
            "       SUM(CASE WHEN image_present=1 THEN 1 ELSE 0 END) AS vision, "
            "       MIN(captured_at) AS oldest, "
            "       MAX(captured_at) AS newest "
            f"FROM training_dataset WHERE {scope}",
            scope_params,
        )
        row = await cursor.fetchone()
        if row is None:
            return {"total": 0, "good": 0, "bad": 0, "unrated": 0,
                    "vision": 0, "oldest": None, "newest": None}
        total = int(row["n"] or 0)
        good = int(row["good"] or 0)
        bad = int(row["bad"] or 0)
        vision = int(row["vision"] or 0)
        # Per-provider breakdown
        cursor = await conn.execute(
            "SELECT provider, COUNT(*) AS n FROM training_dataset "  # noqa: S608
            f"WHERE {scope} GROUP BY provider ORDER BY n DESC LIMIT 10",
            scope_params,
        )
        by_provider = [
            {"provider": r["provider"] or "—", "count": int(r["n"])}
            for r in await cursor.fetchall()
        ]
        # Per-model breakdown
        cursor = await conn.execute(
            "SELECT model, COUNT(*) AS n FROM training_dataset "  # noqa: S608
            f"WHERE {scope} GROUP BY model ORDER BY n DESC LIMIT 10",
            scope_params,
        )
        by_model = [
            {"model": r["model"] or "—", "count": int(r["n"])}
            for r in await cursor.fetchall()
        ]
    return {
        "total": total,
        "good": good,
        "bad": bad,
        "unrated": total - good - bad,
        "vision": vision,
        "oldest": str(row["oldest"]) if row["oldest"] else None,
        "newest": str(row["newest"]) if row["newest"] else None,
        "by_provider": by_provider,
        "by_model": by_model,
    }


async def iter_export_rows(
    *,
    min_rating: int = 0,
    limit: int = 100_000,
) -> list[dict[str, Any]]:
    """Pull rows for a JSONL export. ``min_rating=0`` includes unrated +
    good; ``min_rating=1`` only good (recommended for serious fine-tunes).

    Отдаёт ТОЛЬКО строки владельца (рубеж 2). Чужая строка, каким бы образом
    она в таблице ни оказалась, в файл дообучения не попадает.
    """
    scope, scope_params = await _owner_scope()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT user_text, assistant_text, system_prompt, "  # noqa: S608
            "       context_json, image_present, provider, model, "
            "       rating, captured_at "
            "FROM training_dataset "
            f"WHERE {scope} AND rating >= ? "
            "ORDER BY captured_at ASC LIMIT ?",
            (*scope_params, min_rating, max(1, min(int(limit), 1_000_000))),
        )
        rows = await cursor.fetchall()
    return [
        {
            "messages": (
                ([{"role": "system", "content": r["system_prompt"]}]
                 if r["system_prompt"] else [])
                + (json.loads(r["context_json"]) if r["context_json"] else [])
                + [
                    {"role": "user", "content": str(r["user_text"])},
                    {"role": "assistant", "content": str(r["assistant_text"])},
                ]
            ),
            "metadata": {
                "image_present": bool(int(r["image_present"] or 0)),
                "provider": r["provider"],
                "model": r["model"],
                "rating": int(r["rating"] or 0),
                "captured_at": str(r["captured_at"]),
            },
        }
        for r in rows
    ]
