"""T29 — memory context for chat.

Builds a compact "what I know about the user's recent activity" block from
already-captured data (hourly cards, recent screenshot apps/windows, audio
transcripts) and injects it into the chat so the AI isn't blind. Works even
in lean mode (no workers needed) since it reads existing rows. Once the
embeddings worker is back (step 3c), semantic (query-relevant) retrieval
can be added here too.
"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.memory_context")


async def build_memory_context(query: str = "", *, budget_chars: int = 4000) -> str:
    """Return a compact memory block (or '' if nothing). Time-based recency
    for now; `query` reserved for future semantic retrieval."""
    parts: list[str] = []
    try:
        async with get_connection() as conn:
            # Recent hourly summaries (the richest distilled memory).
            cur = await conn.execute(
                "SELECT hour_start, summary FROM hourly_card "
                "WHERE summary IS NOT NULL AND summary != '' "
                "ORDER BY hour_start DESC LIMIT 4"
            )
            cards = await cur.fetchall()
            if cards:
                lines = [f"  • {str(c['hour_start'])[:16]}: {str(c['summary'])[:400]}" for c in cards]
                parts.append("Сводки последних часов:\n" + "\n".join(lines))

            # Recent apps/windows (works without OCR/workers).
            cur = await conn.execute(
                "SELECT app_name, window_title, MAX(captured_at) AS last "
                "FROM screenshots "
                "WHERE deleted_at IS NULL AND COALESCE(is_private,0)=0 "
                "  AND app_name IS NOT NULL AND app_name != '' "
                "GROUP BY app_name, window_title "
                "ORDER BY last DESC LIMIT 8"
            )
            apps = await cur.fetchall()
            if apps:
                lines = []
                for a in apps:
                    w = (str(a["window_title"]) or "").strip()[:60]
                    lines.append(f"  • {a['app_name']}" + (f" — {w}" if w else ""))
                parts.append("Чем недавно занимался (по экрану):\n" + "\n".join(lines))

            # Recent voice transcripts.
            cur = await conn.execute(
                "SELECT transcript FROM audio_segment "
                "WHERE transcript IS NOT NULL AND transcript != '' "
                "  AND merged_into_id IS NULL "
                "ORDER BY captured_at DESC LIMIT 3"
            )
            auds = await cur.fetchall()
            if auds:
                lines = [f"  • «{str(a['transcript'])[:200]}»" for a in auds]
                parts.append("Недавняя речь (с микрофона):\n" + "\n".join(lines))
    except Exception as exc:  # noqa: BLE001 — memory is best-effort, never break chat
        log.warning("memory_context.failed", error=str(exc))
        return ""

    if not parts:
        return ""
    block = "\n\n── Память: твоя недавняя активность (используй, если уместно) ──\n" + "\n\n".join(parts)
    return block[:budget_chars]
