"""LLM enrichment for tier-5 daily pins (v1.19).

``app.workers.daily_pin_worker`` writes a deterministic micro-summary
for every completed day with ``llm_enriched = 0``. This module is the
opt-in second pass that asks the configured LLM provider for a short
narrative paragraph and stores it on the same row, flipping the flag.

Design rules mirror :mod:`app.llm.card_enricher`:

* **Never invent facts** — the system prompt is explicit and the user
  message contains only the already-on-disk pin text.
* **Never crash on misconfiguration** — :class:`LLMNotConfigured` is
  caught and surfaced as a ``missing_config`` status so the polling
  worker keeps looping.
* **Idempotent** — a second call for a row that is already
  ``llm_enriched = 1`` returns ``already_enriched`` and performs no
  writes.

The wrapper :class:`_UsageRecordingClient` from :mod:`app.llm.client`
records a ``llm_usage`` row with ``kind='daily_pin_enrichment'`` on
every call so the operator can see the cost on ``/stats/llm-usage``.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.daily_pin_enricher")

Status = Literal[
    "ok",
    "already_enriched",
    "missing_config",
    "no_data",
    "error",
]


class EnrichResult(TypedDict):
    """Outcome of one :func:`enrich_pin` call."""

    status: Status
    day: str


_SYSTEM_PROMPT: str = (
    "You are a memory assistant. Given a one-line micro-summary of a "
    "single day, write ONE short narrative paragraph (2-3 sentences) "
    "describing what the user did, in their language. Do not invent "
    "facts. Reply ONLY with the paragraph."
)

#: Cap on the narrative the LLM may emit. 200 tokens is comfortably
#: above 2-3 sentences in either Latin or Cyrillic scripts but keeps the
#: per-call cost predictable when a verbose model decides to ignore the
#: "short" instruction.
_MAX_TOKENS: int = 200

#: Low-creativity temperature — the narrative is grounded in the
#: heuristic pin and we explicitly forbid invention.
_TEMPERATURE: float = 0.3


def _build_user_prompt(pin: str) -> str:
    """Render the user-side message for the LLM call."""
    parts: list[str] = [
        "Daily micro-summary:",
        "",
        pin.strip(),
        "",
        "Write the narrative paragraph now.",
    ]
    return "\n".join(parts)


async def enrich_pin(day_iso: str) -> EnrichResult:
    """Attach an LLM narrative paragraph to the pin row for ``day_iso``.

    Args:
        day_iso: Local-TZ ``YYYY-MM-DD`` primary key of the
            ``daily_pin`` row (e.g. ``'2026-06-04'``).

    Returns:
        :class:`EnrichResult` with ``status``:
          - ``ok`` — narrative stored, flag flipped to 1.
          - ``already_enriched`` — row was already at flag = 1; no-op.
          - ``missing_config`` — LLM not configured; no-op, no crash.
          - ``no_data`` — no row found, or its ``pin`` column is empty.
          - ``error`` — LLM call raised or returned empty text.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT pin, llm_enriched FROM daily_pin WHERE day = ?",
            (day_iso,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.info("daily_pin_enricher.no_data", day=day_iso)
            return {"status": "no_data", "day": day_iso}

        if int(row["llm_enriched"] or 0) == 1:
            log.info("daily_pin_enricher.already_enriched", day=day_iso)
            return {"status": "already_enriched", "day": day_iso}

        pin_text = str(row["pin"] or "").strip()
        if not pin_text:
            log.info("daily_pin_enricher.no_data", day=day_iso, reason="empty_pin")
            return {"status": "no_data", "day": day_iso}

        try:
            client = make_client(kind="daily_pin_enrichment")
        except LLMNotConfigured:
            log.info("daily_pin_enricher.missing_config", day=day_iso)
            return {"status": "missing_config", "day": day_iso}

        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(pin_text),
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
        )

        log.info(
            "daily_pin_enricher.generate.start",
            day=day_iso,
            provider=client.provider,
        )

        try:
            narrative = (await client.complete(request)).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "daily_pin_enricher.generate.failed",
                day=day_iso,
                error=str(exc),
            )
            return {"status": "error", "day": day_iso}

        if not narrative:
            log.warning("daily_pin_enricher.generate.empty", day=day_iso)
            return {"status": "error", "day": day_iso}

        await conn.execute(
            "UPDATE daily_pin "
            "SET llm_narrative = ?, llm_enriched = 1 "
            "WHERE day = ?",
            (narrative, day_iso),
        )
        await conn.commit()

    log.info(
        "daily_pin_enricher.generate.done",
        day=day_iso,
        chars=len(narrative),
    )
    return {"status": "ok", "day": day_iso}


__all__ = ["EnrichResult", "Status", "enrich_pin"]
