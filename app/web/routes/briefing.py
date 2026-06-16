"""Страница проактивного брифинга — /briefing (ROADMAP S3b).

Брифинг = 3-5 карточек из часовой памяти (что делал, что осталось, что логично
сделать). Каждую можно оценить (👍 полезно / 👎 мимо) и скрыть; «мимо»-оценки
копятся и подмешиваются в будущие брифинги как «избегай такого». Кнопка
«Обновить» собирает карточки прямо сейчас. Тихие часы уважаются воркером.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.briefing import (
    build_briefing_cards,
    dismiss_card,
    list_recent_cards,
    set_card_feedback,
    store_cards,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["briefing"])
log = get_logger("persona.briefing.routes")


@router.get("/briefing", response_class=HTMLResponse)
async def briefing_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    cards = await list_recent_cards()
    return templates.TemplateResponse(
        request,
        "briefing.html",
        {
            "title": "Брифинг",
            "active_nav": "briefing",
            "cards": cards,
        },
    )


@router.post("/briefing/refresh", response_model=None)
async def briefing_refresh(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Собрать карточки прямо сейчас (ручной триггер)."""
    try:
        cards = await build_briefing_cards(when="morning")
        n = await store_cards(cards, slot="morning")
        log.info("briefing.manual_refresh", cards=n)
    except Exception as exc:  # noqa: BLE001 — best-effort, не роняем UI
        log.warning("briefing.manual_refresh_failed", error=str(exc))
    return RedirectResponse("/briefing", status_code=303)


@router.post("/briefing/{card_id}/feedback", response_model=None)
async def briefing_feedback(
    card_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    value: int = Form(...),
) -> RedirectResponse:
    """👍 (1) / 👎 (-1) / снять (0)."""
    await set_card_feedback(card_id, value)
    return RedirectResponse("/briefing", status_code=303)


@router.post("/briefing/{card_id}/dismiss", response_model=None)
async def briefing_dismiss(
    card_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    await dismiss_card(card_id)
    return RedirectResponse("/briefing", status_code=303)


__all__ = ["router"]
