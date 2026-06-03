"""Translate a screenshot's OCR text into a target language via BYO LLM.

This is the v0.81 sibling of :mod:`app.llm.ocr_via_vision`. Where the
vision module re-transcribes the thumbnail, this module takes the
*already-OCRed* text in ``screenshots.ocr_text`` and asks the user's
configured BYO LLM to translate it into a language of the user's
choice. The translation is cached in ``screenshots.ocr_text_translated``
alongside the requested language in ``screenshots.ocr_translate_lang``
(see migration ``072_ocr_translate.sql``).

Contract — identical in spirit to ``ocr_via_vision``:

* The Tesseract-owned ``ocr_text`` column is NEVER overwritten — the
  translation is a strict sidecar.
* A cached non-NULL ``ocr_text_translated`` (including an empty string,
  a cached negative result) short-circuits the API call so a second
  click never re-invoices the user.
* Configuration / network / data errors return a status string instead
  of raising. The route layer can render a meaningful message without
  having to wrap the call in ``try / except``.

Unlike vision, this flow is provider-agnostic: any BYO provider that
the :mod:`app.llm.client` factory can construct (Anthropic, OpenAI,
Groq today) is acceptable — translation is a plain text completion,
not multimodal.

Status vocabulary (matches the rest of the LLM surface):

* ``ok``               — translation produced non-empty text (fresh or
                         cached).
* ``empty``            — translation came back blank (cached as ``""``
                         so the next click short-circuits).
* ``no_source``        — the row has no OCR text to translate
                         (``ocr_text`` is ``NULL`` or whitespace).
* ``missing_shot``     — the row does not exist.
* ``missing_config``   — no BYO key / provider configured.
* ``invalid_target``   — ``target_lang`` was empty or whitespace only.
* ``error``            — model raised or some other unexpected failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

import httpx

from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.ocr.translate")

Status = Literal[
    "ok",
    "empty",
    "no_source",
    "missing_shot",
    "missing_config",
    "invalid_target",
    "error",
]


class TranslateResult(TypedDict):
    status: Status
    text: str
    target_lang: str


# The prompt is intentionally terse: text-completion models reliably
# comply with "return only the translation" when the system message
# also reinforces the constraint, and verbose framing just inflates
# the token bill for every shot the user translates. We pass the
# ``target_lang`` value through verbatim so anything the model
# understands (``"ru"``, ``"Russian"``, ``"Español"``, ``"日本語"``) is
# accepted without a server-side allowlist.
_SYSTEM_PROMPT = (
    "You are a faithful translator. The user will send a chunk of text "
    "extracted from a screenshot via OCR. Translate it into the target "
    "language they specify. Preserve line breaks where they obviously "
    "separate UI elements. Return only the translation — no commentary, "
    "no explanations, no source-language echo."
)


def _build_user_prompt(*, target_lang: str, source_text: str) -> str:
    """Compose the user message: "Translate to X. Return only..." + text."""
    return (
        f"Translate the following text to {target_lang}. "
        "Return only the translation.\n\n"
        f"{source_text}"
    )


async def _read_shot_row(
    conn: aiosqlite.Connection, shot_id: int
) -> tuple[str | None, str | None, str | None] | None:
    """Return ``(ocr_text, cached_translation, cached_lang)`` for ``shot_id``.

    Returns ``None`` when the row does not exist. Each tuple slot may
    independently be ``None`` — callers translate that into a status
    code.
    """
    cursor = await conn.execute(
        "SELECT ocr_text, ocr_text_translated, ocr_translate_lang FROM screenshots WHERE id = ?",
        (shot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    ocr_text = None if row["ocr_text"] is None else str(row["ocr_text"])
    cached_text = None if row["ocr_text_translated"] is None else str(row["ocr_text_translated"])
    cached_lang = None if row["ocr_translate_lang"] is None else str(row["ocr_translate_lang"])
    return ocr_text, cached_text, cached_lang


async def _cache_translation(
    conn: aiosqlite.Connection,
    shot_id: int,
    text: str,
    target_lang: str,
) -> None:
    """Persist ``text`` + ``target_lang`` for ``shot_id``.

    Never touches ``ocr_text`` — Tesseract owns that column. Storing
    an empty string is meaningful: it caches a negative result so a
    second click on the admin button doesn't re-invoice the user.
    """
    await conn.execute(
        "UPDATE screenshots SET ocr_text_translated = ?, ocr_translate_lang = ? WHERE id = ?",
        (text, target_lang, shot_id),
    )
    await conn.commit()


def _normalise_target_lang(target_lang: str) -> str | None:
    """Strip + reject empty target-language strings.

    Returns the trimmed value, or ``None`` when the caller supplied
    nothing usable. We don't enforce an allowlist — the value is
    interpolated into the prompt verbatim, so anything the LLM
    understands is fine.
    """
    cleaned = target_lang.strip()
    if not cleaned:
        return None
    return cleaned


def _result(status: Status, text: str, target_lang: str) -> TranslateResult:
    """Tiny helper so the long status-dispatch chain stays one-liner-ish."""
    return {"status": status, "text": text, "target_lang": target_lang}


def _cached_result(cached_text: str, lang: str) -> TranslateResult:
    """Build the ``TranslateResult`` for a cache hit (positive or negative)."""
    status: Status = "ok" if cached_text.strip() else "empty"
    return _result(status, cached_text, lang)


async def _run_completion(
    *,
    shot_id: int,
    source_text: str,
    target_lang: str,
) -> tuple[Status, str]:
    """Call the BYO LLM and return ``(status, cleaned_text)``.

    Wraps :class:`httpx.HTTPError` so the caller only deals with a
    status string. ``make_client`` is re-invoked here (cheap) rather
    than threaded through every helper.
    """
    client = make_client()
    log.info(
        "ocr.translate.call.start",
        shot_id=shot_id,
        provider=client.provider,
        target_lang=target_lang,
        chars=len(source_text),
    )
    try:
        translated = await client.complete(
            CompletionRequest(
                system=_SYSTEM_PROMPT,
                user=_build_user_prompt(
                    target_lang=target_lang,
                    source_text=source_text,
                ),
                max_tokens=1500,
                temperature=0.0,
            )
        )
    except httpx.HTTPError as exc:
        log.warning(
            "ocr.translate.call.http_error",
            shot_id=shot_id,
            provider=client.provider,
            error=str(exc),
        )
        return "error", ""

    cleaned = translated.strip()
    log.info(
        "ocr.translate.call.done",
        shot_id=shot_id,
        provider=client.provider,
        target_lang=target_lang,
        chars=len(cleaned),
    )
    status: Status = "ok" if cleaned else "empty"
    return status, cleaned


async def translate_shot(shot_id: int, target_lang: str) -> TranslateResult:
    """Translate the shot's OCR text into ``target_lang`` via the BYO LLM.

    Idempotent: if a previous pass already cached a result for *any*
    target language, the cached value is returned with no API call —
    we don't multiplex multiple target languages onto one row because
    the v0.81 schema only carries a single ``ocr_text_translated``
    column. The admin page surfaces only rows where the translation is
    ``NULL``, so the typical cache-hit case is "user re-clicked the
    same row" and we want that to be cheap.

    The Tesseract-owned ``ocr_text`` column is NEVER written from this
    code path.

    Configuration problems and transient errors return a status string
    rather than raising, so the route layer can render a meaningful
    message without try / except plumbing.
    """
    normalised_lang = _normalise_target_lang(target_lang)
    if normalised_lang is None:
        log.info("ocr.translate.invalid_target", shot_id=shot_id)
        return _result("invalid_target", "", "")

    try:
        make_client()
    except LLMNotConfigured:
        log.info("ocr.translate.no_config", shot_id=shot_id)
        return _result("missing_config", "", normalised_lang)

    async with get_connection() as conn:
        row = await _read_shot_row(conn, shot_id)
        if row is None:
            log.warning("ocr.translate.missing_shot", shot_id=shot_id)
            return _result("missing_shot", "", normalised_lang)
        ocr_text, cached_text, cached_lang = row

        # Cache hit (including cached negative). Either way, no API call.
        if cached_text is not None:
            log.info(
                "ocr.translate.cache.hit",
                shot_id=shot_id,
                cached_lang=cached_lang or "",
                requested_lang=normalised_lang,
                length=len(cached_text),
            )
            return _cached_result(cached_text, cached_lang or normalised_lang)

        source_text = (ocr_text or "").strip()
        if not source_text:
            log.info("ocr.translate.no_source", shot_id=shot_id)
            return _result("no_source", "", normalised_lang)

        status, cleaned = await _run_completion(
            shot_id=shot_id,
            source_text=source_text,
            target_lang=normalised_lang,
        )
        if status != "error":
            # Only persist successful + cached-negative passes; an
            # ``error`` status (HTTP / transport failure) leaves the
            # row's translation NULL so the user can retry later.
            await _cache_translation(conn, shot_id, cleaned, normalised_lang)
        return _result(status, cleaned if status != "error" else "", normalised_lang)
