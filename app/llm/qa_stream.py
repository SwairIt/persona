"""Streaming variant of :mod:`app.llm.qa`.

Mirrors :func:`app.llm.qa.ask` but uses :meth:`LLMClient.stream` so the
caller can forward each text delta to the UI as Server-Sent Events
instead of waiting for the full answer (5-15 s on Gemini Flash).

The retrieval and prompt-building logic are reused as-is from
:mod:`app.llm.qa` — only the LLM call is replaced. Each yielded dict has
a ``type`` key so the SSE route can serialise without further shaping:

* ``{"type": "meta", "used_screenshots": N, "citations_seed": [...]}``
  — emitted exactly once before any text.
* ``{"type": "delta", "text": <chunk>}`` — zero or more text chunks.
* ``{"type": "done", "full_answer": <str>, "citations": [...]}`` —
  emitted exactly once at the end, even when the answer was empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.llm.client import CompletionRequest, LLMClient, make_client
from app.llm.qa import (
    _QA_SYSTEM,
    _build_prompt,
    _extract_citations,
    _gather_context,
)
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.qa_stream")


async def stream_answer(
    question: str,
    top_k: int = 10,
    client: LLMClient | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE-ready dicts for one Q&A request.

    Raises :class:`ValueError` synchronously if the question is empty so
    the HTTP layer can return a 400 before opening the stream.
    """
    question = question.strip()
    if not question:
        msg = "Empty question"
        raise ValueError(msg)

    context = await _gather_context(question, top_k=top_k)
    if not context:
        # T20 (2026-06-07): same fallback as non-streaming /ask —
        # answer as a general assistant when no captures match.
        yield {"type": "meta", "used_screenshots": 0, "citations_seed": []}
        llm = client or make_client(kind="qa_stream")
        general_prompt = (
            "Ты — Persona, локальный AI-помощник. У тебя есть доступ к "
            "истории скриншотов пользователя, но для этого вопроса ничего "
            "релевантного не нашлось. Ответь по существу как обычный "
            "помощник.\n\n"
            f"Вопрос: {question}"
        )
        chunks_g: list[str] = []
        try:
            async for delta in llm.stream(
                CompletionRequest(
                    system=_QA_SYSTEM,
                    user=general_prompt,
                    max_tokens=600,
                ),
            ):
                if not delta:
                    continue
                chunks_g.append(delta)
                yield {"type": "delta", "text": delta}
        except Exception as exc:
            log.warning("qa_stream.fallback_failed", error=str(exc))
        yield {
            "type": "done",
            "full_answer": "".join(chunks_g),
            "citations": [],
        }
        return

    # Surface candidate ids up front so the UI can prime its "cited"
    # affordance before the model finishes. The final ``done`` event
    # carries the de-duplicated, model-filtered citation set.
    valid_ids: set[int] = set()
    for ctx_item in context:
        raw_id = ctx_item.get("id")
        if isinstance(raw_id, int):
            valid_ids.add(raw_id)
    yield {
        "type": "meta",
        "used_screenshots": len(context),
        "citations_seed": sorted(valid_ids),
    }

    llm = client or make_client(kind="qa_stream")
    prompt = _build_prompt(question, context)
    request = CompletionRequest(system=_QA_SYSTEM, user=prompt, max_tokens=600)

    chunks: list[str] = []
    try:
        async for delta in llm.stream(request):
            if not delta:
                continue
            chunks.append(delta)
            yield {"type": "delta", "text": delta}
    except Exception as exc:
        log.warning("qa_stream.failed", error=str(exc))
        yield {
            "type": "done",
            "full_answer": "".join(chunks),
            "citations": [],
            "error": str(exc),
        }
        return

    full_answer = "".join(chunks)
    citations = sorted(_extract_citations(full_answer, valid_ids=valid_ids))
    log.info(
        "qa_stream.done",
        used_screenshots=len(context),
        answer_len=len(full_answer),
        citations=len(citations),
    )
    yield {
        "type": "done",
        "full_answer": full_answer,
        "citations": citations,
    }
