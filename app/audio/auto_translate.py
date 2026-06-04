"""Voice-segment auto-translate (v1.18).

Whisper transcribes ``audio_segment.transcript`` in whatever language the
speaker actually used. The Persona UI is one of a small set of locales
(today: ``en`` / ``ru``). If a user records a call in German they can't
search it from a Russian UI without a translated sidecar.

This module fills :col:`audio_segment.transcript_translated` (and the
sister hint :col:`audio_segment.source_language`) lazily, via the user's
BYO LLM. The translation is cached per row so the worker, the manual
admin button, and the search reindexer never re-bill the user for the
same segment twice.

Status vocabulary (mirrors the rest of the LLM surface):

* ``ok``               — translation produced and persisted.
* ``already_target``   — detected source language matched the UI
                         language; no API call needed.
* ``no_text``          — the row has no ``transcript`` to translate.
* ``missing_config``   — no BYO key / provider configured.
* ``missing_segment``  — the row does not exist.
* ``error``            — model raised or some other unexpected failure
                         (the row's translation stays NULL so the worker
                         can retry on the next tick).

All paths return a :class:`TranslateResult` dict — the calling worker
and route layers never have to wrap the call in ``try / except`` for
configuration / network problems, only for genuinely exceptional bugs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

import httpx

from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.audio.auto_translate")

Status = Literal[
    "ok",
    "already_target",
    "no_text",
    "missing_config",
    "missing_segment",
    "error",
]


class TranslateResult(TypedDict):
    """Outcome of one :func:`translate_segment` call."""

    status: Status
    seg_id: int
    source_language: str | None


# The translator prompt is intentionally terse — text-completion models
# reliably comply with "reply only with the translation" when the system
# message reinforces the constraint. We interpolate the user's UI
# language (``en`` / ``ru`` today) verbatim; the LLM understands both
# language codes and English names.
_SYSTEM_PROMPT_TEMPLATE = (
    "You are a translator. Translate the following text into "
    "{target_lang}. Reply ONLY with the translation, no preamble."
)


async def detect_language(text: str) -> str | None:
    """Best-effort language detection for ``text``.

    Uses the ``langdetect`` package when it's installed; returns
    ``None`` otherwise (and logs at INFO so the operator can spot the
    missing optional dependency without grepping the warning bucket).

    The function is async even though the underlying ``langdetect`` call
    is synchronous — keeping the signature awaitable lets the worker
    treat detection and translation uniformly and keeps the door open
    for a future async detector backend.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        import langdetect  # noqa: PLC0415 — optional dependency
    except ImportError:
        log.info("audio.auto_translate.langdetect_missing")
        return None

    try:
        detected = langdetect.detect(cleaned)
    except Exception as exc:
        # ``langdetect.LangDetectException`` is the documented failure
        # mode, but we catch broad here because the package can also
        # raise ``ValueError`` on degenerate input (single character,
        # whitespace-only after its own normalisation, etc.).
        log.warning(
            "audio.auto_translate.langdetect_failed",
            error=str(exc),
            chars=len(cleaned),
        )
        return None

    code = str(detected).strip().lower() or None
    log.info("audio.auto_translate.langdetect_ok", language=code)
    return code


async def _read_segment_row(
    conn: aiosqlite.Connection,
    seg_id: int,
) -> tuple[str | None, str | None, str | None] | None:
    """Return ``(transcript, transcript_translated, source_language)``.

    ``None`` when the row does not exist. Each tuple slot may
    independently be ``None`` — callers translate that into a status.
    """
    cursor = await conn.execute(
        "SELECT transcript, transcript_translated, source_language "
        "FROM audio_segment WHERE id = ?",
        (seg_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    transcript = None if row["transcript"] is None else str(row["transcript"])
    translated = (
        None if row["transcript_translated"] is None else str(row["transcript_translated"])
    )
    source_lang = None if row["source_language"] is None else str(row["source_language"])
    return transcript, translated, source_lang


async def _update_source_language(
    conn: aiosqlite.Connection,
    seg_id: int,
    language: str,
) -> None:
    """Persist the detected ``source_language`` for ``seg_id``."""
    await conn.execute(
        "UPDATE audio_segment SET source_language = ? WHERE id = ?",
        (language, seg_id),
    )
    await conn.commit()


async def _update_translation(
    conn: aiosqlite.Connection,
    seg_id: int,
    translation: str,
) -> None:
    """Persist the translated text for ``seg_id``.

    The Whisper-owned ``transcript`` column is NEVER overwritten — the
    translation is a strict sidecar in ``transcript_translated``.
    """
    await conn.execute(
        "UPDATE audio_segment SET transcript_translated = ? WHERE id = ?",
        (translation, seg_id),
    )
    await conn.commit()


def _result(status: Status, seg_id: int, source_language: str | None) -> TranslateResult:
    """Tiny helper so the dispatch chain stays one-liner-ish."""
    return {"status": status, "seg_id": seg_id, "source_language": source_language}


async def _run_completion(
    *,
    seg_id: int,
    transcript: str,
    target_lang: str,
) -> tuple[Status, str]:
    """Call the BYO LLM and return ``(status, cleaned_text)``.

    Wraps :class:`httpx.HTTPError` so the caller only deals with a
    status string. Configuration is rechecked here so a key removal
    between the outer guard and the actual call still falls through
    gracefully.
    """
    try:
        client = make_client(kind="audio.auto_translate")
    except LLMNotConfigured:
        log.info("audio.auto_translate.no_config", seg_id=seg_id)
        return "missing_config", ""

    log.info(
        "audio.auto_translate.call.start",
        seg_id=seg_id,
        provider=client.provider,
        target_lang=target_lang,
        chars=len(transcript),
    )
    try:
        translated = await client.complete(
            CompletionRequest(
                system=_SYSTEM_PROMPT_TEMPLATE.format(target_lang=target_lang),
                user=transcript,
                max_tokens=1500,
                temperature=0.0,
            )
        )
    except httpx.HTTPError as exc:
        log.warning(
            "audio.auto_translate.call.http_error",
            seg_id=seg_id,
            provider=client.provider,
            error=str(exc),
        )
        return "error", ""
    except Exception as exc:
        # Any non-HTTP raise (provider SDK regressions, JSON parse, ...)
        # is logged with exception traceback once and downgraded to a
        # status string so the worker loop keeps going.
        log.exception(
            "audio.auto_translate.call.failed",
            seg_id=seg_id,
            provider=client.provider,
            error=str(exc),
        )
        return "error", ""

    cleaned = translated.strip()
    log.info(
        "audio.auto_translate.call.done",
        seg_id=seg_id,
        provider=client.provider,
        target_lang=target_lang,
        chars=len(cleaned),
    )
    return "ok", cleaned


async def translate_segment(seg_id: int, target_lang: str) -> TranslateResult:
    """Translate the segment's transcript into ``target_lang``.

    Resolution:

    1. Read the row. Missing → ``missing_segment``.
    2. ``transcript`` empty / whitespace → ``no_text``.
    3. ``source_language`` NULL → detect via :func:`detect_language`,
       persist if found.
    4. ``source_language == target_lang`` → ``already_target`` (no API
       call, no row write).
    5. BYO LLM not configured → ``missing_config``.
    6. Call the LLM, persist the translation, return ``ok``.

    Configuration / network failures return a status string rather than
    raising so the worker loop and the route layer never need to wrap
    the call in ``try / except``.
    """
    async with get_connection() as conn:
        row = await _read_segment_row(conn, seg_id)
        if row is None:
            log.warning("audio.auto_translate.missing_segment", seg_id=seg_id)
            return _result("missing_segment", seg_id, None)

        transcript_raw, _translated_existing, source_lang = row

        transcript = (transcript_raw or "").strip()
        if not transcript:
            log.info("audio.auto_translate.no_text", seg_id=seg_id)
            return _result("no_text", seg_id, source_lang)

        # Lazy language detection: populate the column on the first
        # attempt so subsequent ticks can short-circuit cheaply.
        if source_lang is None:
            detected = await detect_language(transcript)
            if detected is not None:
                await _update_source_language(conn, seg_id, detected)
                source_lang = detected

        normalised_target = target_lang.strip().lower()
        if source_lang is not None and source_lang.strip().lower() == normalised_target:
            log.info(
                "audio.auto_translate.already_target",
                seg_id=seg_id,
                source_language=source_lang,
                target_lang=normalised_target,
            )
            return _result("already_target", seg_id, source_lang)

        status, cleaned = await _run_completion(
            seg_id=seg_id,
            transcript=transcript,
            target_lang=target_lang,
        )
        if status == "ok":
            await _update_translation(conn, seg_id, cleaned)
        return _result(status, seg_id, source_lang)


__all__ = [
    "Status",
    "TranslateResult",
    "detect_language",
    "translate_segment",
]
