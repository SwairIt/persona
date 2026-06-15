"""T29 — memory context for chat.

Builds a compact "what I know about the user's recent activity" block from
already-captured data (hourly cards, recent screenshot apps/windows, audio
transcripts) and injects it into the chat so the AI isn't blind. Works even
in lean mode (no workers needed) since it reads existing rows. Once the
embeddings worker is back (step 3c), semantic (query-relevant) retrieval
can be added here too.
"""

from __future__ import annotations

import re

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.memory_context")

# Слова-признаки, что вопрос ПРО недавнюю активность пользователя — тогда
# уместно подмешать память дня целиком (по свежести). Иначе — только то, что
# реально совпадает с запросом, чтобы не засорять каждый ответ нерелевантным
# контекстом (главная претензия к «памяти» ChatGPT — Simon Willison).
_ACTIVITY_HINTS = (
    "вчера", "сегодня", "утром", "недавно", "час", "часа", "часов",
    "что я", "чем я", "над чем", "делал", "занимал", "работал", "смотрел",
    "экран", "приложен", "сейчас", "только что", "напомн", "помн",
    "recent", "yesterday", "today", "what was i", "what did i", "screen",
    "earlier", "remind",
)


def _query_terms(q: str) -> list[str]:
    """Слова запроса (>=4 симв.) для проверки релевантности по совпадению."""
    return [w for w in re.findall(r"[0-9A-Za-zА-Яа-яЁё]{4,}", q.casefold())][:12]


def _filter_lines(lines: list[tuple[str, str]], terms: list[str]) -> list[str]:
    """Оставить строки, чей текст содержит хотя бы один термин запроса."""
    out: list[str] = []
    for text, rendered in lines:
        tl = text.casefold()
        if any(t in tl for t in terms):
            out.append(rendered)
    return out


async def build_memory_context(query: str = "", *, budget_chars: int = 4000) -> str:
    """Компактный блок памяти дня — ТОЛЬКО когда релевантно вопросу.

    Если вопрос про активность (см. _ACTIVITY_HINTS) — отдаём свежие карточки/
    приложения/речь по свежести. Иначе — только пункты, совпадающие со словами
    запроса; если совпадений нет — пустая строка (не засоряем ответ).
    """
    q = (query or "").casefold()
    intent = any(h in q for h in _ACTIVITY_HINTS)
    terms = _query_terms(query)
    if not intent and not terms:
        return ""

    cards_lines: list[tuple[str, str]] = []
    apps_lines: list[tuple[str, str]] = []
    aud_lines: list[tuple[str, str]] = []
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT hour_start, summary FROM hourly_card "
                "WHERE summary IS NOT NULL AND summary != '' "
                "ORDER BY hour_start DESC LIMIT 8"
            )
            for c in await cur.fetchall():
                text = str(c["summary"])
                cards_lines.append((text, f"  • {str(c['hour_start'])[:16]}: {text[:400]}"))

            cur = await conn.execute(
                "SELECT app_name, window_title, MAX(captured_at) AS last "
                "FROM screenshots "
                "WHERE deleted_at IS NULL AND COALESCE(is_private,0)=0 "
                "  AND app_name IS NOT NULL AND app_name != '' "
                "GROUP BY app_name, window_title ORDER BY last DESC LIMIT 16"
            )
            for a in await cur.fetchall():
                w = (str(a["window_title"]) or "").strip()[:60]
                text = f"{a['app_name']} {w}"
                apps_lines.append((text, f"  • {a['app_name']}" + (f" — {w}" if w else "")))

            cur = await conn.execute(
                "SELECT transcript FROM audio_segment "
                "WHERE transcript IS NOT NULL AND transcript != '' "
                "  AND merged_into_id IS NULL ORDER BY captured_at DESC LIMIT 8"
            )
            for a in await cur.fetchall():
                text = str(a["transcript"])
                aud_lines.append((text, f"  • «{text[:200]}»"))
    except Exception as exc:  # noqa: BLE001 — memory is best-effort, never break chat
        log.warning("memory_context.failed", error=str(exc))
        return ""

    if intent:
        cards = [r for _, r in cards_lines[:4]]
        apps = [r for _, r in apps_lines[:8]]
        auds = [r for _, r in aud_lines[:3]]
    else:
        cards = _filter_lines(cards_lines, terms)[:4]
        apps = _filter_lines(apps_lines, terms)[:6]
        auds = _filter_lines(aud_lines, terms)[:3]

    parts: list[str] = []
    if cards:
        parts.append("Сводки последних часов:\n" + "\n".join(cards))
    if apps:
        parts.append("Чем недавно занимался (по экрану):\n" + "\n".join(apps))
    if auds:
        parts.append("Недавняя речь (с микрофона):\n" + "\n".join(auds))
    if not parts:
        return ""
    used = len(cards) + len(apps) + len(auds)
    log.debug("memory_context.injected", items=used, intent=intent, terms=len(terms))
    block = (
        "\n\n── Память: твоя недавняя активность (опирайся ТОЛЬКО если относится "
        "к вопросу; не выдумывай сверх этого) ──\n" + "\n\n".join(parts)
    )
    return block[:budget_chars]
