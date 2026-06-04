"""LLM enrichment for tier-1 hourly cards (v1.14).

``app.hourly_card`` writes a deterministic markdown card for every
completed hour with ``llm_enriched = 0``. This module is the opt-in
second pass that asks the configured LLM provider for a short narrative
paragraph and appends it to the existing summary, flipping the flag.

Design rules:

* **Never invent facts** — the system prompt is explicit and the user
  message contains only the data we already have on disk (the rendered
  summary plus the truncated transcript excerpt).
* **Never crash on misconfiguration** — :class:`LLMNotConfigured` is
  caught and surfaced as a ``missing_config`` status. The worker keeps
  looping in that case.
* **Idempotent** — a second call for a row that is already
  ``llm_enriched = 1`` returns ``already_enriched`` and performs no
  writes. The worker can call this for the same hour twice without
  doubling up the narrative.

The wrapper :class:`_UsageRecordingClient` from :mod:`app.llm.client`
records a ``llm_usage`` row with ``kind='card_enrichment'`` on every
call so the operator can see the cost on ``/stats/llm-usage``.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.card_enricher")

Status = Literal[
    "ok",
    "already_enriched",
    "missing_config",
    "no_data",
    "error",
]


class EnrichResult(TypedDict):
    """Outcome of one :func:`enrich_card` call."""

    status: Status
    hour_start: str


_SYSTEM_PROMPT: str = (
    'You are a memory assistant. Given a structured hourly summary, '
    'write ONE short narrative paragraph (2-3 sentences) describing '
    "what the user did, in their language. Do not invent facts. "
    'Reply ONLY with the paragraph.'
)

#: Cap on the narrative the LLM is allowed to emit. 200 tokens is
#: comfortably above 2-3 sentences in either Latin or Cyrillic scripts
#: but keeps the per-call cost predictable when a verbose model decides
#: to ignore the "short" instruction.
_MAX_TOKENS: int = 200

#: Low-creativity temperature — the narrative is grounded in the
#: structured summary and we explicitly forbid invention.
_TEMPERATURE: float = 0.3


def _build_user_prompt(summary: str, transcript_excerpt: str | None) -> str:
    """Render the user-side message for the LLM call.

    The structured summary already contains the apps / keywords /
    screen count block produced by :mod:`app.hourly_card`. The
    transcript excerpt is appended verbatim when present so the LLM
    has voice context but is never asked to expand beyond it.
    """
    parts: list[str] = ['Structured hourly summary:', '', summary.strip()]
    if transcript_excerpt and transcript_excerpt.strip():
        parts.extend(
            [
                '',
                'Transcript excerpt:',
                transcript_excerpt.strip(),
            ],
        )
    parts.extend(
        [
            '',
            'Write the narrative paragraph now.',
        ],
    )
    return '\n'.join(parts)


async def enrich_card(hour_start: str) -> EnrichResult:
    """Add an LLM narrative paragraph to the card at ``hour_start``.

    Args:
        hour_start: ISO UTC timestamp that is the primary key of the
            ``hourly_card`` row (e.g. ``'2026-06-04T14:00:00+00:00'``).

    Returns:
        ``EnrichResult`` with ``status``:
          - ``ok`` — narrative appended, flag flipped to 1.
          - ``already_enriched`` — row was already at flag = 1; no-op.
          - ``missing_config`` — LLM not configured; no-op, no crash.
          - ``no_data`` — no row found for ``hour_start``.
          - ``error`` — LLM call raised or returned empty text.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            'SELECT summary, transcript_excerpt, llm_enriched '
            'FROM hourly_card WHERE hour_start = ?',
            (hour_start,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.info('card_enricher.no_data', hour_start=hour_start)
            return {'status': 'no_data', 'hour_start': hour_start}

        if int(row['llm_enriched'] or 0) == 1:
            log.info('card_enricher.already_enriched', hour_start=hour_start)
            return {
                'status': 'already_enriched',
                'hour_start': hour_start,
            }

        original_summary = str(row['summary'] or '')
        transcript_raw = row['transcript_excerpt']
        transcript_excerpt: str | None = (
            str(transcript_raw) if transcript_raw is not None else None
        )

        try:
            client = make_client(kind='card_enrichment')
        except LLMNotConfigured:
            log.info('card_enricher.missing_config', hour_start=hour_start)
            return {'status': 'missing_config', 'hour_start': hour_start}

        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(original_summary, transcript_excerpt),
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
        )

        log.info(
            'card_enricher.generate.start',
            hour_start=hour_start,
            provider=client.provider,
        )

        try:
            narrative = (await client.complete(request)).strip()
        except Exception as exc:
            log.warning(
                'card_enricher.generate.failed',
                hour_start=hour_start,
                error=str(exc),
            )
            return {'status': 'error', 'hour_start': hour_start}

        if not narrative:
            log.warning(
                'card_enricher.generate.empty',
                hour_start=hour_start,
            )
            return {'status': 'error', 'hour_start': hour_start}

        await conn.execute(
            'UPDATE hourly_card '
            'SET summary = summary || char(10) || char(10) || ?, '
            '    llm_enriched = 1 '
            'WHERE hour_start = ?',
            (narrative, hour_start),
        )
        await conn.commit()

    log.info(
        'card_enricher.generate.done',
        hour_start=hour_start,
        chars=len(narrative),
    )
    return {'status': 'ok', 'hour_start': hour_start}


__all__ = ['EnrichResult', 'Status', 'enrich_card']
