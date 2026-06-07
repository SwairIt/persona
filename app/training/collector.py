"""Append Q&A pairs to ``training_dataset`` after each chat turn.

Designed to be cheap and forgiving:
  * If the kv flag is off → no-op without error.
  * If insert raises (table missing on a fresh install before the
    migration ran) → log a warning and move on. The chat reply still
    reaches the user.
"""

from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

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
    on no-op / failure."""
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
                "  (session_id, user_message_id, asst_message_id, "
                "   user_text, assistant_text, system_prompt, "
                "   context_json, image_present, provider, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
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


async def stats() -> dict[str, Any]:
    """Counts for the /admin/dataset dashboard."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n, "
            "       SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END) AS good, "
            "       SUM(CASE WHEN rating=-1 THEN 1 ELSE 0 END) AS bad, "
            "       SUM(CASE WHEN image_present=1 THEN 1 ELSE 0 END) AS vision, "
            "       MIN(captured_at) AS oldest, "
            "       MAX(captured_at) AS newest "
            "FROM training_dataset"
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
            "SELECT provider, COUNT(*) AS n FROM training_dataset "
            "GROUP BY provider ORDER BY n DESC LIMIT 10"
        )
        by_provider = [
            {"provider": r["provider"] or "—", "count": int(r["n"])}
            for r in await cursor.fetchall()
        ]
        # Per-model breakdown
        cursor = await conn.execute(
            "SELECT model, COUNT(*) AS n FROM training_dataset "
            "GROUP BY model ORDER BY n DESC LIMIT 10"
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
    good; ``min_rating=1`` only good (recommended for serious fine-tunes)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT user_text, assistant_text, system_prompt, "
            "       context_json, image_present, provider, model, "
            "       rating, captured_at "
            "FROM training_dataset "
            "WHERE rating >= ? "
            "ORDER BY captured_at ASC LIMIT ?",
            (min_rating, max(1, min(int(limit), 1_000_000))),
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
