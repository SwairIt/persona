"""SSE-эндпоинт встроенного копилота Persona (слайс B2).

``GET /api/copilot/ask?q=&page_url=&mode=`` стримит ответ ИИ как
Server-Sent Events, оборачивая :func:`app.llm.copilot_stream.stream_copilot`
в ``text/event-stream``. Формат кадра — как в /api/ask/stream::

    data: {"type": "delta", "text": "..."}\\n\\n

Гейты (роль решает, какой из них применяется):

* Авторизация — ``current_user_required`` (копилот привязан к пользователю,
  его память/настройки не для анонимов).
* ВЛАДЕЛЕЦ — мастер-флаг «ИИ везде» (kv ``ai_everywhere``). Выключен → один
  event ``{type:'error', reason:'disabled'}`` и закрываем стрим (200 OK, чтобы
  браузерный EventSource открыл канал и увидел причину, а не завис). Это тот
  же гейт, что у остальных owner-поверхностей ИИ (dashboard_ai, search_ai,
  timeline_ai, ai_calendar).
* УЧАСТНИК — наличие СВОЕЙ модели (:func:`user_llm_configured`: собственный
  провайдер или явно выданная другом квота). Нет модели → один event
  ``{type:'error', reason:'llm_not_configured', href:'/settings/llm'}``.

Раньше тут стоял один гейт на всех — ``is_owner``, — и любой участник получал
кадр ``disabled`` с подписью «режим ИИ везде выключен» и ссылкой на
owner-only страницу ``/settings/ai-everywhere``, которую он не может открыть.
То есть копилот врал о причине и вёл в тупик. Мастер-флаг владельца НЕ
управляет копилотом участника: это настройка чужого аккаунта.
"""

# ruff: noqa: RUF002

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord  # noqa: TC001 - FastAPI inspects it
from app.llm.client import LLMNotConfigured, user_llm_configured
from app.llm.copilot_stream import LLM_NOT_CONFIGURED_EVENT, stream_copilot
from app.logging_setup import get_logger
from app.web.routes.ai_everywhere_settings import is_ai_everywhere

# Fail-closed резолв роли: сбой резолва = «участник» (app/web/routes/owner_view.py).
from app.web.routes.owner_view import viewer_is_owner as is_owner

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.copilot.route")

router = APIRouter(tags=["copilot"])
_HEARTBEAT_SECONDS = 10.0


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
    iterator = stream_copilot(
        question, page_url=page_url, mode=mode, user_id=user_id
    ).__aiter__()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait(
                {pending},
                timeout=_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                # SSE comments are ignored by EventSource but keep nginx and
                # upstream middleware alive while the slow PC worker prepares
                # its first token.
                yield b": persona-copilot-ping\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            pending = None
            yield _encode_sse(event)
    except LLMNotConfigured as exc:
        log.info("copilot.route.llm_offline", error=str(exc))
        yield _encode_sse(
            {"type": "error", "reason": "llm_offline", "message": "ПК-воркер офлайн"}
        )
    except Exception as exc:
        log.warning("copilot.route.failed", error=str(exc))
        yield _encode_sse({"type": "error", "reason": "internal", "detail": str(exc)})
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        close = getattr(iterator, "aclose", None)
        if callable(close):
            with contextlib.suppress(BaseException):
                await close()


async def _single_event_stream(event: dict[str, Any]) -> AsyncIterator[bytes]:
    """Стрим из одного event-а: причина отказа вместо пустого канала."""
    yield _encode_sse(event)


async def _copilot_gate(user_id: int) -> dict[str, Any] | None:
    """Решение гейта: ``None`` — пускаем, dict — отдаём этот кадр и всё.

    Сбой резолва владельца трактуем как «участник»: хуже открыть участнику
    owner-путь, чем показать владельцу подсказку про свою модель.
    """
    try:
        owner = await is_owner(user_id)
    except Exception as exc:  # noqa: BLE001 — сбой гейта → урезанная роль
        log.warning("copilot.route.owner_resolve_failed", error=str(exc))
        owner = False

    if owner:
        # Владелец: ровно тот гейт, что у остальных его ИИ-поверхностей.
        return None if await is_ai_everywhere() else {"type": "error", "reason": "disabled"}

    # Участник: единственная настоящая причина отказа — нет своей модели.
    # ``user_llm_configured`` учитывает и одолженную другом (app/llm/grants.py)
    # и никогда не бросает.
    if await user_llm_configured(user_id):
        return None
    return dict(LLM_NOT_CONFIGURED_EVENT)


@router.get("/api/copilot/ask")
async def copilot_ask(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    q: str = "",
    page_url: str = "",
    mode: str = "ask",
) -> StreamingResponse:
    """Стрим ответа копилота как Server-Sent Events.

    Владелец → мастер-флаг «ИИ везде»; участник → своя подключённая модель
    (см. :func:`_copilot_gate`). Отказ — всегда один event с честной причиной
    и 200 OK, чтобы EventSource показал текст, а не молча ретраил.

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
    refusal = await _copilot_gate(user_id)
    if refusal is not None:
        return StreamingResponse(
            _single_event_stream(refusal),
            media_type="text/event-stream",
            headers=headers,
        )
    return StreamingResponse(
        _event_stream(q, page_url, mode, user_id),
        media_type="text/event-stream",
        headers=headers,
    )


__all__ = ["copilot_ask", "router"]
