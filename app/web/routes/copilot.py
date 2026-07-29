"""SSE-эндпоинт встроенного копилота Persona (слайс B2).

``GET /api/copilot/ask?q=&page_url=&mode=`` стримит ответ ИИ как
Server-Sent Events, оборачивая :func:`app.llm.copilot_stream.stream_copilot`
в ``text/event-stream``. Формат кадра — как в /api/ask/stream::

    data: {"type": "delta", "text": "..."}\\n\\n

Гейты:
* Авторизация — ``current_user_required`` (копилот привязан к пользователю,
  его память/настройки не для анонимов).
* Мастер-флаг «ИИ везде» — если выключен, отдаём один event
  ``{type:'error', reason:'disabled'}`` и закрываем стрим (200 OK, чтобы
  браузерный EventSource открыл канал и увидел причину, а не завис).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.llm.client import LLMNotConfigured
from app.llm.copilot_stream import stream_copilot
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.copilot.route")

router = APIRouter(tags=["copilot"])


def _encode_sse(event: dict[str, Any]) -> bytes:
    """Закодировать один event в SSE-формат ``data: <json>\\n\\n``."""
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    return f"data: {body}\n\n".encode()


async def _event_stream(
    question: str, page_url: str, mode: str, user_id: int | None
) -> AsyncIterator[bytes]:
    """Перевести вывод :func:`stream_copilot` в SSE-кадры.

    ``LLMNotConfigured`` ловим наверху стрима (на случай, если оно всплывёт
    мимо мягкой обработки внутри генератора) и отдаём понятный error-event,
    а не 500. Прочие исключения логируем и тоже отдаём событием, чтобы UI
    показал сообщение, а не завис на открытом соединении.
    """
    try:
        async for event in stream_copilot(
            question, page_url=page_url, mode=mode, user_id=user_id
        ):
            yield _encode_sse(event)
    except LLMNotConfigured as exc:
        log.info("copilot.route.llm_offline", error=str(exc))
        yield _encode_sse(
            {"type": "error", "reason": "llm_offline", "message": "ПК-воркер офлайн"}
        )
    except Exception as exc:  # noqa: BLE001 — не роняем соединение в 500
        log.warning("copilot.route.failed", error=str(exc))
        yield _encode_sse({"type": "error", "reason": "internal", "detail": str(exc)})


async def _disabled_stream() -> AsyncIterator[bytes]:
    """Единственный event при выключенном мастер-флаге «ИИ везде»."""
    yield _encode_sse({"type": "error", "reason": "disabled"})


@router.get("/api/copilot/ask")
async def copilot_ask(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    q: str = "",
    page_url: str = "",
    mode: str = "ask",
) -> StreamingResponse:
    """Стрим ответа копилота как Server-Sent Events.

    Query-параметры:
        q: вопрос/намерение пользователя.
        page_url: URL текущей страницы (для режимов summary/ask).
        mode: 'ask' | 'summary' | 'find_setting' (деф. 'ask').
    """
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    user_id = int(session["user_id"])
    if not await is_owner(user_id):
        return StreamingResponse(
            _disabled_stream(),
            media_type="text/event-stream",
            headers=headers,
        )
    return StreamingResponse(
        _event_stream(q, page_url, mode, user_id),
        media_type="text/event-stream",
        headers=headers,
    )


__all__ = ["copilot_ask", "router"]
