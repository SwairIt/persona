"""Hands-free голосовой чат — /voice.

Полноэкранный «орб»-микрофон: говоришь → распознаётся (Web Speech API) →
уходит в существующий чат (send-stream) → ответ озвучивается (speechSynthesis).
Режим hands-free: после ответа снова слушает. Барж-ин: тап во время речи
ассистента прерывает озвучку.

Чисто клиентская страница поверх существующих API (сессии чата + send-stream +
браузерные STT/TTS). Сервер только отдаёт шаблон. Транскрипция и синтез — в
браузере (для приватности и нулевой задержки); серверный Whisper-fallback
(/api/voice/web/stt) подключим при необходимости.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.web.templates_engine import templates

router = APIRouter(tags=["voice"])


@router.get("/voice", response_class=HTMLResponse)
async def voice_chat_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "voice_chat.html",
        {"title": "Голос — разговор с ассистентом", "active_nav": "voice-live"},
    )
