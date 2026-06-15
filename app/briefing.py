"""Проактивная сводка дня — наш моат против Hermes.

Hermes помнит только то, что ты ему НАПЕЧАТАЛ. Persona помнит, что ты реально
ДЕЛАЛ (скрины+OCR+аудио → часовые карточки). Из этого собираем короткую
дружелюбную сводку и проактивно показываем (утром — план/что вчера осталось,
вечером — итоги дня). Best-effort: нет данных/LLM → деградируем мягко.
"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.storage.db import get_connection

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
