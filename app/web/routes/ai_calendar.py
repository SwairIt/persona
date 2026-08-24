"""ИИ-календарь — напоминания на естественном языке (срез C1).

Часть мастер-режима «ИИ везде» (фундамент A1). Два эндпоинта:

* ``POST /api/ai-calendar/parse``  — распарсить фразу («напомни завтра …»)
  в ``{body, due_date, matched_date}`` для предпросмотра, БЕЗ записи в БД.
* ``POST /api/ai-calendar/create`` — распарсить и СОЗДАТЬ напоминание в
  таблице ``reminders``.

Парсер дат — детерминированный :func:`app.chat.reminder_nl.parse_reminder`
(быстрый, без LLM, покрыт тестами). Если он не распознал дату
(``matched_date=False``), пробуем усилить LLM-воркером через ``complete_json``
по JSON-схеме — но это best-effort: при недоступности LLM тихо остаёмся на
детерминированном результате (сегодня), а не падаем 500.

Гейт: оба эндпоинта живут только при включённом «ИИ везде» — иначе 404.
Авторизация — ``current_user_required``.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.chat.reminder_nl import parse_reminder
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.reminders import create_reminder
from app.web.routes.ai_everywhere_settings import is_ai_everywhere

router = APIRouter(tags=["ai-calendar"])

log = get_logger("persona.web.ai_calendar")


class _TextPayload(BaseModel):
    """Тело запроса: свободная фраза пользователя."""

    text: str = Field(..., min_length=1, max_length=2000)


async def _gate() -> None:
    """Благородный отказ, если мастер-режим «ИИ везде» выключен → 404."""
    if not await is_ai_everywhere():
        raise HTTPException(status_code=404, detail="AI features disabled")


async def _parse_nl(text: str) -> dict[str, Any]:
    """NL → {body, due_date, matched_date}.

    Основной путь — детерминированный parse_reminder. Если он не нашёл дату,
    пробуем усилить LLM-воркером (structured JSON). LLM недоступен/ошибся →
    молча возвращаем детерминированный результат (не 500).
    """
    text = (text or "").strip()
    parsed = parse_reminder(text)
    if parsed.get("matched_date"):
        return parsed

    # Дата не распознана правилами — пробуем LLM (best-effort усиление).
    try:
        from app.llm.client import (  # noqa: PLC0415
            CompletionRequest,
            LLMNotConfigured,
            make_client,
        )

        client = make_client(kind="copilot")
        if not hasattr(client, "complete_json"):
            return parsed  # провайдер без structured-вывода → остаёмся на правилах
        today_iso = date.today().isoformat()
        req = CompletionRequest(
            system=(
                "Ты извлекаешь напоминание из фразы пользователя. Верни суть "
                "задачи (body) и дату (due_date) в формате YYYY-MM-DD. Сегодня "
                f"{today_iso}. Если даты во фразе нет — поставь сегодняшнюю. "
                "Пиши только по-русски."
            ),
            user=text,
            max_tokens=200,
        )
        schema = {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "due_date": {"type": "string"},
            },
            "required": ["body", "due_date"],
        }
        try:
            out = await client.complete_json(req, schema)  # type: ignore[attr-defined]
        except LLMNotConfigured as exc:
            log.info("ai_calendar.llm_unavailable", error=str(exc))
            return parsed
        body = str(out.get("body") or "").strip() or parsed["body"]
        due_raw = str(out.get("due_date") or "").strip()
        try:
            due = date.fromisoformat(due_raw)
        except ValueError:
            return parsed  # LLM вернул кривую дату → доверяем правилам
        return {"body": body, "due_date": due.isoformat(), "matched_date": True}
    except Exception as exc:  # noqa: BLE001 — LLM не должен ломать парсинг
        log.warning("ai_calendar.parse_llm_failed", error=str(exc))
        return parsed


@router.post("/api/ai-calendar/parse", response_class=JSONResponse)
async def ai_calendar_parse(
    payload: _TextPayload,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Предпросмотр: распарсить фразу в {body, due_date, matched_date}."""
    await _gate()
    parsed = await _parse_nl(payload.text)
    return JSONResponse(
        {
            "ok": True,
            "body": parsed["body"],
            "due_date": parsed["due_date"],
            "matched_date": bool(parsed.get("matched_date")),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/ai-calendar/create", response_class=JSONResponse)
async def ai_calendar_create(
    payload: _TextPayload,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Распарсить фразу и создать напоминание в таблице reminders."""
    await _gate()
    parsed = await _parse_nl(payload.text)
    body = str(parsed.get("body") or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")
    try:
        due = date.fromisoformat(parsed["due_date"])
        async with get_connection() as conn:
            reminder_id = await create_reminder(conn, body=body, due_date=due)
    except Exception as exc:  # noqa: BLE001
        log.warning("ai_calendar.create_failed", error=str(exc))
        raise HTTPException(
            status_code=500, detail="Не удалось сохранить напоминание"
        ) from exc
    log.info("ai_calendar.created", id=reminder_id, due=parsed["due_date"])
    return JSONResponse(
        {
            "ok": True,
            "reminder_id": reminder_id,
            "body": body,
            "due_date": parsed["due_date"],
        },
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
