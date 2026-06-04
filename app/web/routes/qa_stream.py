"""Streaming Q&A endpoint — Server-Sent Events variant of /api/ask.

The blocking POST /api/ask returns the whole answer once the LLM is
done, which on Gemini Flash takes 5-15 seconds and looks frozen in the
UI. This route streams each text delta as it arrives by wrapping
:func:`app.llm.qa_stream.stream_answer` in an ``EventSource``-friendly
``text/event-stream`` response.

Wire format: each yielded event from :func:`stream_answer` is encoded
as one SSE frame::

    data: {"type": "delta", "text": "..."}\\n\\n

When the LLM is not configured we still return ``200 OK`` so the
browser's ``EventSource`` opens the channel; the first (and only)
event carries ``{"type": "error", "reason": "missing_config"}`` and
the stream closes. This keeps the JS reload path symmetrical with
the success case.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from starlette.responses import StreamingResponse

from app.llm.client import LLMNotConfigured
from app.llm.qa_stream import stream_answer
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.qa_stream.route")

router = APIRouter(tags=["qa"])


def _encode_sse(event: dict[str, Any]) -> bytes:
    """Encode one event dict in the SSE ``data: <json>\\n\\n`` wire format."""
    return f"data: {json.dumps(event, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


async def _event_stream(question: str, top_k: int) -> AsyncIterator[bytes]:
    """Translate :func:`stream_answer` output into SSE frames.

    Catches :class:`LLMNotConfigured` at the top of the stream so the
    browser still sees a clean ``200`` response with a single error
    event. Other exceptions are logged and surfaced as an ``error``
    event before the stream closes, so the UI can show a message
    instead of the connection just hanging.
    """
    try:
        async for event in stream_answer(question, top_k=top_k):
            yield _encode_sse(event)
    except LLMNotConfigured as exc:
        log.info("qa_stream.route.not_configured", error=str(exc))
        yield _encode_sse({"type": "error", "reason": "missing_config"})
    except ValueError as exc:
        log.info("qa_stream.route.bad_request", error=str(exc))
        yield _encode_sse({"type": "error", "reason": "bad_request", "detail": str(exc)})
    except Exception as exc:
        log.warning("qa_stream.route.failed", error=str(exc))
        yield _encode_sse({"type": "error", "reason": "internal", "detail": str(exc)})


@router.get("/api/ask/stream")
async def ask_stream(q: str, top_k: int = 10) -> StreamingResponse:
    """Stream a Q&A answer as Server-Sent Events.

    Query params:
        q: the user's question (required).
        top_k: number of context items to retrieve (default 10).
    """
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _event_stream(q, top_k),
        media_type="text/event-stream",
        headers=headers,
    )


__all__ = ["ask_stream", "router"]
