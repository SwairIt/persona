"""Per-shot LLM alt-text — one-line description cached on ``screenshots``.

Tesseract gives us :data:`screenshots.ocr_text` — a wall of half-extracted
character fragments. That's mediocre as alt-text and useless as a
scrolling-timeline glance line. This module asks the user's BYO LLM
to summarise the OCR into a single sub-80-char sentence
("Cursor editor showing Python function definition with type hints")
and caches the result in :data:`screenshots.alt_text` (see migration
``108_shot_alt_text.sql``).

Contract
--------
* The Tesseract-owned ``ocr_text`` column is NEVER overwritten — the
  description is a strict sidecar.
* A cached non-NULL ``alt_text`` short-circuits the API call so a
  second click / worker pass never re-invoices the user.
* Rows with no OCR text (NULL / empty / whitespace) skip the API
  entirely and return ``no_ocr`` — there's nothing meaningful to
  summarise.
* Configuration / network / data errors return a status string instead
  of raising. The route layer can render a meaningful message without
  having to wrap the call in ``try / except``.

Status vocabulary (matches the rest of the LLM surface):

* ``ok``               — fresh description produced and persisted.
* ``already_set``      — ``alt_text`` was non-NULL on entry; nothing to do.
* ``no_ocr``           — the row has no OCR text to summarise
                         (``ocr_text`` is ``NULL`` or whitespace).
* ``missing_shot``     — the row does not exist.
* ``missing_config``   — no BYO key / provider configured.
* ``error``            — model raised or some other unexpected failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypedDict

import httpx

from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.shot_alt_text")

Status = Literal[
    "ok",
    "already_set",
    "no_ocr",
    "missing_shot",
    "missing_config",
    "error",
]


class AltTextResult(TypedDict):
    status: Status
    alt_text: str


# The system prompt is intentionally short and prescriptive. We want
# the model to (a) emit exactly one sentence, (b) stay under 80 chars,
# (c) match the OCR's own language so a Russian screenshot doesn't get
# an English caption (and vice versa). Re-asking for "no greeting,
# no quotes" cuts down on chatty completions that would otherwise burn
# tokens for nothing.
_SYSTEM_PROMPT = (
    "You write one-line image descriptions for a personal memory app. "
    "Given OCR text from a screenshot, write a single sentence "
    "(under 80 chars) describing what the user was doing or looking at. "
    "No greeting, no quotes. Reply in the language of the OCR text."
)

#: Cap on the OCR slice we pass to the model. The OCR column can be
#: tens of kilobytes for a dense IDE screenshot; the first ~1000 chars
#: are more than enough signal for a one-line summary and keep the
#: per-call token cost predictable.
_MAX_OCR_CHARS: int = 1000


def _result(status: Status, alt_text: str) -> AltTextResult:
    """Tiny helper so the status-dispatch chain stays one-liner-ish."""
    return {"status": status, "alt_text": alt_text}


async def generate_alt_text(shot_id: int) -> AltTextResult:
    """Generate + cache a one-line LLM description for ``shot_id``.

    Idempotent: returns ``already_set`` without an API call when the
    row already has a non-NULL ``alt_text``. Rows without OCR text
    short-circuit to ``no_ocr`` — there's nothing meaningful to
    summarise so we don't waste a BYO-key call.

    The Tesseract-owned ``ocr_text`` column is NEVER written from this
    code path.

    Configuration problems and transient errors return a status string
    rather than raising, so the worker / route layer can render a
    meaningful message without try / except plumbing.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text, alt_text FROM screenshots WHERE id = ?",
            (shot_id,),
        )
        row = await cursor.fetchone()

    if row is None:
        log.warning("shot_alt_text.missing_shot", shot_id=shot_id)
        return _result("missing_shot", "")

    cached_alt: str | None = None if row["alt_text"] is None else str(row["alt_text"])
    ocr_text: str | None = None if row["ocr_text"] is None else str(row["ocr_text"])

    if cached_alt is not None and cached_alt.strip():
        log.info("shot_alt_text.already_set", shot_id=shot_id)
        return _result("already_set", cached_alt)

    source_text = (ocr_text or "").strip()
    if not source_text:
        log.info("shot_alt_text.no_ocr", shot_id=shot_id)
        return _result("no_ocr", "")

    try:
        make_client(kind="shot_alt_text")
    except LLMNotConfigured:
        log.info("shot_alt_text.no_config", shot_id=shot_id)
        return _result("missing_config", "")

    status, cleaned = await _run_completion(shot_id, source_text)
    if status == "ok":
        await _persist(shot_id, cleaned)
    return _result(status, cleaned)


async def _run_completion(
    shot_id: int, source_text: str
) -> tuple[Status, str]:
    """Call the BYO LLM and return ``(status, cleaned_text)``.

    Wraps :class:`httpx.HTTPError` and bare ``Exception`` so the caller
    only deals with a status string — a transient network blip can
    never crash the worker. An empty / whitespace-only completion
    collapses to ``("error", "")`` so the row's ``alt_text`` stays NULL
    and the worker retries on a future tick.
    """
    client = make_client(kind="shot_alt_text")
    trimmed = source_text[:_MAX_OCR_CHARS]
    log.info(
        "shot_alt_text.call.start",
        shot_id=shot_id,
        provider=client.provider,
        chars=len(trimmed),
    )
    try:
        completion = await client.complete(
            CompletionRequest(
                system=_SYSTEM_PROMPT,
                user=trimmed,
                max_tokens=120,
                temperature=0.2,
            )
        )
    except httpx.HTTPError as exc:
        log.warning(
            "shot_alt_text.call.http_error",
            shot_id=shot_id,
            provider=client.provider,
            error=str(exc),
        )
        return "error", ""
    except Exception as exc:
        log.warning(
            "shot_alt_text.call.failed",
            shot_id=shot_id,
            provider=client.provider,
            error=str(exc),
        )
        return "error", ""

    cleaned = completion.strip().strip('"').strip("'").strip()
    if not cleaned:
        log.info("shot_alt_text.empty_completion", shot_id=shot_id)
        return "error", ""

    log.info(
        "shot_alt_text.call.done",
        shot_id=shot_id,
        provider=client.provider,
        chars=len(cleaned),
    )
    return "ok", cleaned


async def _persist(shot_id: int, alt_text: str) -> None:
    """Write ``alt_text`` + ``alt_text_generated_at`` for ``shot_id``.

    Never touches ``ocr_text`` — Tesseract owns that column. Stamps the
    write time as a UTC ISO-8601 string so ``/stats`` and a future
    "stale model" pass can find rows generated by an older model.
    """
    generated_at = datetime.now(tz=UTC).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE screenshots SET alt_text = ?, alt_text_generated_at = ? "
            "WHERE id = ?",
            (alt_text, generated_at, shot_id),
        )
        await conn.commit()


__all__ = ["AltTextResult", "Status", "generate_alt_text"]
