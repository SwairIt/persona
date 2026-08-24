"""ИИ-саммари часа на Ленте (СЛАЙС E1) — приколюха «✨ саммари часа».

Для каждой группы-часа на /timeline генерим ОДНО предложение о том, чем человек
занимался в этот час, опираясь на OCR-текст и список приложений этого часа.
Считает вездесущий копилот (``make_client(kind="copilot")`` — провайдер
``worker``, модель на ПК, ключ не нужен). Гейт мастер-режима «ИИ везде»: при OFF
эндпоинт отдаёт 404, а не тратит LLM впустую.

Результат кэшируем в ``kv_settings`` по iso-ключу часа: одинаковый час не
пересчитываем (LLM на ПК дорогой по латентности). Кэш складываем даже для
пустых часов, чтобы не долбить модель на «нет данных».
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request  # noqa: F401 — Request для симметрии
from fastapi.responses import JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.storage.time import iso as to_iso
from app.web.routes.ai_everywhere_settings import is_ai_everywhere
from app.web.templates_engine import resolve_display_tz

router = APIRouter(prefix="/api/timeline", tags=["timeline-ai"])
log = get_logger("persona.timeline_ai")

#: Бюджет символов OCR-контекста для модели. Слабый локальный GPU легко
#: захлёбывается длинным промптом, поэтому режем вход жёстко (~2000).
_OCR_BUDGET = 2000

#: Префикс kv-ключа кэша. Полный ключ — ``timeline_hour_summary:<iso>``.
_CACHE_PREFIX = "timeline_hour_summary:"

_SYSTEM = (
    "Ты кратко описываешь, чем человек занимался в течение ОДНОГО часа, опираясь "
    "ТОЛЬКО на список приложений и фрагменты текста с его экрана (OCR). Ответь "
    "РОВНО ОДНИМ коротким предложением по-русски, без вступлений и списков. Не "
    "выдумывай фактов сверх данных."
)


def _parse_hour(value: str) -> datetime:
    """iso часа (в зоне отображения) → tz-aware datetime начала часа.

    Принимаем как ``2026-07-01T14:00`` (локальная зона отображения), так и
    полный ISO с оффсетом. Значение усекаем до начала часа. Мусор → 400.
    """
    raw = (value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный формат часа") from exc
    if parsed.tzinfo is None:
        # Без оффсета трактуем как зону отображения ленты (как рендерятся часы).
        parsed = parsed.replace(tzinfo=resolve_display_tz())
    # Усечь до начала часа — саммари считается по бакету [hour, hour+1).
    return parsed.replace(minute=0, second=0, microsecond=0)


async def _read_cache(conn, key: str) -> str | None:
    try:
        return await get_kv(conn, key)
    except Exception:  # noqa: BLE001 — кэш не критичен
        return None


async def _write_cache(conn, key: str, value: str) -> None:
    try:
        await set_kv(conn, key, value)
    except Exception as exc:  # noqa: BLE001 — не смогли закэшировать → просто отдадим ответ
        log.debug("timeline_ai.cache_write_failed", error=str(exc))


async def _hour_context(conn, since: datetime, until: datetime) -> tuple[str, list[str]]:
    """Собрать (OCR-сэмпл ≤ бюджета, список приложений) для окна [since, until)."""
    apps_cur = await conn.execute(
        "SELECT app_name, COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL "
        "GROUP BY app_name ORDER BY n DESC LIMIT 8",
        (to_iso(since), to_iso(until)),
    )
    apps = [str(r["app_name"]) for r in await apps_cur.fetchall()]

    ocr_cur = await conn.execute(
        "SELECT ocr_text FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "AND ocr_text IS NOT NULL AND ocr_text != '' "
        "ORDER BY captured_at LIMIT 60",
        (to_iso(since), to_iso(until)),
    )
    ocr_blob = "\n".join(str(r["ocr_text"]) for r in await ocr_cur.fetchall())
    return ocr_blob[:_OCR_BUDGET].strip(), apps


@router.get("/hour/{iso}/summary", response_class=JSONResponse, response_model=None)
async def hour_summary(
    iso: str,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Одно предложение про час → ``{"summary": "..."}``. Кэш по iso в kv."""
    # Гейт мастер-режима «ИИ везде»: при OFF фича не существует.
    if not await is_ai_everywhere():
        raise HTTPException(status_code=404, detail="ИИ-режим выключен")

    hour_start = _parse_hour(iso)
    hour_end = hour_start + timedelta(hours=1)
    # Ключ кэша — нормализованный (усечённый до часа) ISO, чтобы «14:00» и
    # «14:37» одного часа делили один кэш-энтри.
    cache_key = _CACHE_PREFIX + hour_start.isoformat()

    async with get_connection() as conn:
        cached = await _read_cache(conn, cache_key)
        if cached is not None:
            return JSONResponse({"summary": cached}, headers={"Cache-Control": "no-store"})

        ocr_blob, apps = await _hour_context(conn, hour_start, hour_end)

        if not ocr_blob and not apps:
            summary = "За этот час нет данных для сводки."
            await _write_cache(conn, cache_key, summary)
            return JSONResponse({"summary": summary}, headers={"Cache-Control": "no-store"})

        # LLM зовём вне транзакции чтения — но соединение из get_connection()
        # уже наше; закрываем его до сетевого вызова, затем открываем новое
        # только для записи кэша, чтобы не держать коннект во время долгого
        # локального инференса на ПК.

    # --- LLM (копилот) ---
    from app.llm.client import (  # noqa: PLC0415 — ленивый импорт, не тянем LLM в старте
        CompletionRequest,
        LLMNotConfigured,
        make_client,
    )

    try:
        client = make_client(kind="copilot")
    except LLMNotConfigured:
        # Благородный отказ, не 500: фича не настроена — просто нет саммари.
        return JSONResponse(
            {"summary": "", "error": "LLM не настроен."},
            headers={"Cache-Control": "no-store"},
        )

    context_parts: list[str] = [f"Час: {hour_start.strftime('%H:00')}."]
    if apps:
        context_parts.append("Приложения: " + ", ".join(apps) + ".")
    if ocr_blob:
        context_parts.append("Фрагменты с экрана (OCR):\n" + ocr_blob)
    req = CompletionRequest(
        system=_SYSTEM,
        user="\n".join(context_parts),
        max_tokens=120,
        temperature=0.4,
    )

    try:
        summary = (await client.complete(req)).strip()
    except LLMNotConfigured:
        return JSONResponse(
            {"summary": "", "error": "LLM недоступен."},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:  # noqa: BLE001 — сеть/ПК-воркер → не 500, а мягко
        log.warning("timeline_ai.summary_failed", error=str(exc))
        return JSONResponse(
            {"summary": "", "error": "Не удалось получить сводку."},
            headers={"Cache-Control": "no-store"},
        )

    if not summary:
        summary = "За этот час не удалось составить сводку."

    # Кэшируем удачный (и «пустой удачный») результат по iso.
    async with get_connection() as conn:
        await _write_cache(conn, cache_key, summary)

    return JSONResponse({"summary": summary}, headers={"Cache-Control": "no-store"})


__all__ = ["router"]
