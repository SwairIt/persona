"""Проактивная сводка дня — наш моат против Hermes.

Hermes помнит только то, что ты ему НАПЕЧАТАЛ. Persona помнит, что ты реально
ДЕЛАЛ (скрины+OCR+аудио → часовые карточки). Из этого собираем короткую
дружелюбную сводку и проактивно показываем (утром — план/что вчера осталось,
вечером — итоги дня). Best-effort: нет данных/LLM → деградируем мягко.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction

log = get_logger("persona.briefing")


async def build_briefing(*, when: str = "morning") -> tuple[str, str] | None:
    """Вернуть (заголовок, текст) сводки или None, если данных нет.

    Источник — последние часовые карточки (richest distilled memory). LLM
    делает из них короткую человеческую сводку; без LLM — отдаём сырой дайджест.
    """
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT hour_start, summary FROM hourly_card "
            "WHERE summary IS NOT NULL AND summary != '' "
            "ORDER BY hour_start DESC LIMIT 16"
        )
        rows = await cur.fetchall()
    if not rows:
        return None
    # хронологический порядок (старые → новые) для читабельности
    digest = "\n".join(f"- {str(r['summary'])[:300]}" for r in reversed(rows))

    title = "🌅 Утренняя сводка" if when == "morning" else "🌙 Итоги дня"
    from app.llm.client import (  # noqa: PLC0415 — избегаем цикла импорта
        CompletionRequest,
        LLMNotConfigured,
        make_client,
    )

    try:
        client = make_client(kind="chat_summary")
    except LLMNotConfigured:
        return (title, digest[:1500])
    if when == "morning":
        system = (
            "Ты — личный ассистент. Сделай КОРОТКУЮ дружелюбную утреннюю сводку "
            "(3–6 пунктов) по недавней активности пользователя: над чем работал, "
            "что важное осталось незакрытым, что логично сделать сегодня. По-русски, "
            "по делу, без воды и без подхалимажа."
        )
    else:
        system = (
            "Ты — личный ассистент. Сделай КОРОТКИЕ дружелюбные итоги дня (3–6 "
            "пунктов): что сделано, что осталось, на чём остановился. По-русски, "
            "по делу, без воды."
        )
    user = f"Активность (часовые карточки в хронологии):\n{digest}\n\nСводка:"
    try:
        text = await client.complete(
            CompletionRequest(system=system, user=user, max_tokens=420, temperature=0.4)
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("briefing.llm_failed", error=str(exc))
        return (title, digest[:1500])
    body = (text or "").strip() or digest
    return (title, body[:2000])


# ── S3b: брифинг в виде карточек с обратной связью ────────────────────────────

# Схема для GBNF (Ollama complete_json) — форсит валидный JSON, режет CJK-мусор.
_CARDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "icon": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["cards"],
}

_MAX_CARDS = 5


async def _recent_disliked_titles(limit: int = 8) -> list[str]:
    """Заголовки недавно «мимо»-карточек — чтобы будущий брифинг их избегал."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT title FROM briefing_card WHERE feedback = -1 "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [str(r["title"]) for r in await cur.fetchall()]


async def build_briefing_cards(*, when: str = "morning") -> list[dict[str, str]]:
    """Собрать 3-5 карточек брифинга из часовой памяти. [] если данных нет.

    Каждая карточка — {icon,title,body}. GBNF-путь (Ollama) даёт надёжный JSON;
    для облака/сбоя — fallback: дробим текстовую сводку на пункты-карточки.
    Учитываем «мимо»-фидбек прошлых карточек (не повторять отвергнутое).
    """
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT summary FROM hourly_card "
            "WHERE summary IS NOT NULL AND summary != '' "
            "ORDER BY hour_start DESC LIMIT 16"
        )
        rows = await cur.fetchall()
    if not rows:
        return []
    digest = "\n".join(f"- {str(r['summary'])[:300]}" for r in reversed(rows))
    avoid = await _recent_disliked_titles()
    avoid_hint = (
        "\nПользователю НЕ зашли такие темы (не повторяй их формулировки): "
        + "; ".join(avoid)
        if avoid
        else ""
    )

    from app.llm.client import (  # noqa: PLC0415
        CompletionRequest,
        LLMNotConfigured,
        make_client,
    )

    when_word = "утренний план" if when == "morning" else "итоги дня"
    system = (
        "Ты — личный ассистент. По активности пользователя собери "
        f"{when_word}: 3-5 КОРОТКИХ карточек. Каждая — конкретный пункт с "
        "эмодзи-иконкой, кратким заголовком (до 7 слов) и одним предложением "
        "пояснения. По делу, по-русски, без воды и подхалимажа." + avoid_hint
    )
    user = f"Активность (часовые карточки в хронологии):\n{digest}"

    try:
        client = make_client(kind="chat_summary")
    except LLMNotConfigured:
        return _fallback_cards(digest, when)

    cards: list[dict[str, str]] = []
    if hasattr(client, "complete_json"):
        try:
            out = await client.complete_json(
                CompletionRequest(
                    system=system + " Верни JSON {cards:[{icon,title,body}]}.",
                    user=user, max_tokens=600, temperature=0.4,
                ),
                _CARDS_SCHEMA,
            )
            for c in (out.get("cards") or []):
                if isinstance(c, dict) and str(c.get("title", "")).strip():
                    cards.append(
                        {
                            "icon": (str(c.get("icon") or "•").strip() or "•")[:4],
                            "title": str(c["title"]).strip()[:120],
                            "body": str(c.get("body") or "").strip()[:400],
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            log.debug("briefing.cards_json_failed", error=str(exc))

    if not cards:
        return _fallback_cards(digest, when)
    return cards[:_MAX_CARDS]


def _fallback_cards(digest: str, when: str) -> list[dict[str, str]]:
    """Без LLM/JSON — дробим сырой дайджест на пункты-карточки."""
    icon = "🌅" if when == "morning" else "🌙"
    cards: list[dict[str, str]] = []
    for line in digest.splitlines():
        text = line.strip().lstrip("-•*").strip()
        if len(text) < 6:
            continue
        cards.append({"icon": icon, "title": text[:80], "body": text[80:280]})
        if len(cards) >= _MAX_CARDS:
            break
    return cards


async def store_cards(cards: list[dict[str, str]], *, slot: str = "morning") -> int:
    """Сохранить свежие карточки (заменяя прошлый набор того же слота за сегодня)."""
    if not cards:
        return 0
    async with write_transaction() as conn:
        # Не плодить дубли: убрать сегодняшние карточки этого слота перед вставкой.
        await conn.execute(
            "DELETE FROM briefing_card WHERE slot = ? "
            "AND date(created_at) = date('now')",
            (slot,),
        )
        for c in cards:
            await conn.execute(
                "INSERT INTO briefing_card(slot, icon, title, body) VALUES(?,?,?,?)",
                (slot, c.get("icon", "•"), c.get("title", ""), c.get("body", "")),
            )
    return len(cards)


async def list_recent_cards(limit: int = 12) -> list[dict[str, Any]]:
    """Активные (не скрытые) карточки для страницы /briefing."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, slot, icon, title, body, feedback, created_at "
            "FROM briefing_card WHERE dismissed = 0 "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": int(r["id"]),
                "slot": str(r["slot"]),
                "icon": str(r["icon"]),
                "title": str(r["title"]),
                "body": str(r["body"]),
                "feedback": int(r["feedback"]),
                "created_at": str(r["created_at"]),
            }
            for r in await cur.fetchall()
        ]


async def set_card_feedback(card_id: int, value: int) -> None:
    """👍 (1) / 👎 (-1) / снять (0)."""
    value = 1 if value > 0 else (-1 if value < 0 else 0)
    async with write_transaction() as conn:
        await conn.execute(
            "UPDATE briefing_card SET feedback = ? WHERE id = ?", (value, card_id)
        )


async def dismiss_card(card_id: int) -> None:
    async with write_transaction() as conn:
        await conn.execute(
            "UPDATE briefing_card SET dismissed = 1 WHERE id = ?", (card_id,)
        )
