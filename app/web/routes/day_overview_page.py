"""Единая страница ДНЯ — /day/{date} (BUILD_PLAN A2).

Сводит воедино то, что раньше было размазано по timeline/scrubber/collage/stats:
KPI дня (скрины, был ли записан звук, сколько использовался ИИ, часы активности),
TL;DR, топ-приложения, часовые карточки памяти и галерею скринов по часам. Отсюда
же — «спросить про этот день» (A3) и быстрые ссылки на остальные виды дня.
Owner/кабинет: под current_user_required.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.day_overview import day_bounds_utc, get_day_overview
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.web.routes.timeline import _day_bounds, _group_by_hour, _parse_date
from app.web.templates_engine import templates

router = APIRouter(tags=["day"])
log = get_logger("persona.day_page")

_MAX_SHOTS = 600
_OCR_SAMPLE_CHARS = 6000


@router.get("/day/{date}", response_class=HTMLResponse, response_model=None)
async def day_page(
    request: Request,
    date: str,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Обзор одного дня: статистика + скрины по часам + карточки + действия."""
    day = _parse_date(date)  # дружелюбно: мусор → сегодня
    day_iso = day.strftime("%Y-%m-%d")
    overview = await get_day_overview(day_iso, user_id=session["user_id"])

    since, until = _day_bounds(day)
    async with get_connection() as conn:
        shots = await list_screenshots(conn, limit=_MAX_SHOTS, since=since, until=until)
        tags_by_id: dict = {}
        try:
            from app.storage.tags import get_tags_for_many  # noqa: PLC0415

            tags_by_id = await get_tags_for_many(conn, [s.id for s in shots])
        except Exception:  # noqa: BLE001 — теги не критичны для страницы дня
            tags_by_id = {}

    groups = _group_by_hour(shots)
    return templates.TemplateResponse(
        request,
        "day_overview.html",
        {
            "title": f"День · {day_iso}",
            "active_nav": "day",
            "ov": overview,
            "day_iso": day_iso,
            "groups": groups,
            "tags_by_id": tags_by_id,
            "shots_shown": len(shots),
            "shots_capped": len(shots) >= _MAX_SHOTS,
        },
    )


_ASK_SYSTEM = (
    "Ты отвечаешь на вопрос пользователя про КОНКРЕТНЫЙ день его жизни, опираясь "
    "ТОЛЬКО на предоставленные данные (статистика дня, часовые карточки памяти, "
    "фрагменты текста с экрана). Не выдумывай факты сверх данных; если в данных "
    "ответа нет — честно скажи, что за этот день таких данных не видно. Отвечай "
    "по-русски, кратко и по делу."
)


async def _day_context(day_iso: str, user_id: int | None) -> str:
    """Собрать текстовый контекст дня для LLM: статистика + карточки + сэмпл OCR."""
    ov = await get_day_overview(day_iso, user_id=user_id)
    parts: list[str] = [
        f"Дата: {day_iso}.",
        f"Скриншотов: {ov['screenshots']} (OCR: {ov['ocr_done']}), активных часов: "
        f"{ov['active_hours']}, звук: {ov['audio_minutes']} мин, использований ИИ: "
        f"{ov['ai_uses']} (токенов {ov['total_tokens']}).",
    ]
    if ov.get("tldr"):
        parts.append(f"Краткая сводка дня: {ov['tldr']}")
    if ov.get("top_apps"):
        parts.append("Топ приложений: " + ", ".join(
            f"{a['app']}({a['count']})" for a in ov["top_apps"]))
    if ov.get("hourly_cards"):
        parts.append("Память по часам:")
        for c in ov["hourly_cards"]:
            parts.append(f"[{c['hour_start'][11:16]}] {c['summary'][:500]}")

    # сэмпл OCR-текста за день (обрезаем по бюджету символов)
    since, until = day_bounds_utc(day_iso)
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT ocr_text FROM screenshots WHERE captured_at >= ? AND captured_at < ? "
                "AND ocr_text IS NOT NULL AND ocr_text != '' ORDER BY captured_at LIMIT 120",
                (since, until))
            rows = await cur.fetchall()
        ocr_blob = "\n".join(str(r[0]) for r in rows)[:_OCR_SAMPLE_CHARS]
        if ocr_blob.strip():
            parts.append("Фрагменты с экрана (OCR):\n" + ocr_blob)
    except Exception as exc:  # noqa: BLE001
        log.debug("day_ask.ocr_failed", error=str(exc))
    return "\n".join(parts)


async def answer_about_day(
    day_iso: str, user_id: int | None, question: str, client: Any = None
) -> dict[str, Any]:
    """LLM-ответ про день. client можно инжектить (тесты). Graceful без LLM."""
    from app.llm.client import CompletionRequest, LLMNotConfigured, make_client  # noqa: PLC0415

    question = (question or "").strip()
    if not question:
        return {"answer": "", "status": "empty"}
    try:
        ll = client or make_client(kind="chat")
    except LLMNotConfigured:
        return {"answer": "LLM не настроен — открой /settings/llm и выбери провайдера.",
                "status": "missing_config"}
    context = await _day_context(day_iso, user_id)
    req = CompletionRequest(
        system=_ASK_SYSTEM,
        user=f"Данные за день:\n{context}\n\nВопрос: {question}",
        max_tokens=600,
        temperature=0.3,
    )
    try:
        text = (await ll.complete(req)).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("day_ask.failed", error=str(exc))
        return {"answer": f"Не удалось получить ответ: {exc}", "status": "error"}
    return {"answer": text or "(пустой ответ)", "status": "ok"}


@router.post("/api/day/{date}/ask", response_model=None)
async def day_ask(
    date: str,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict, Body(...)],
) -> JSONResponse:
    """Спросить ИИ про конкретный день (контекст = статистика+карточки+OCR дня)."""
    day = _parse_date(date)
    day_iso = day.strftime("%Y-%m-%d")
    question = str(body.get("question", "")).strip()
    result = await answer_about_day(day_iso, session["user_id"], question)
    return JSONResponse(result)


__all__ = ["router", "answer_about_day", "_day_context"]
