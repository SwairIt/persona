"""Smart-pin LLM auto-flagger (v1.50).

Once a day a worker calls :func:`suggest_smart_pins` for yesterday's
date. The function:

1. Picks up to ``max_candidates`` screenshots from that day whose tier
   is *not* already ``pinned`` and whose OCR text is non-empty.
2. Trims the candidate set to a phash-diverse sample: at most one
   shot per ``(app_name, window_title)`` pair, so the model sees a
   spread of contexts rather than 30 thumbnails of the same VS Code
   window.
3. Asks the configured BYO LLM "which of these look IMPORTANT?" with
   a strict JSON-array contract. Important = code-review approvals,
   decisions, key events — exactly the kind of moment a user would
   manually pin if they were paying attention.
4. Persists each pick as a row in ``smart_pin_suggestion``. Acceptance
   is a separate user action (route ``/api/smart-pin/{id}/accept``);
   this module never flips the real ``screenshots.tier``.

Same guardrails as :mod:`app.llm.daily_pin_enricher`:

* Missing BYO key → :class:`LLMNotConfigured` is caught and surfaces
  as a ``missing_config`` status. The worker keeps looping.
* Empty / malformed JSON → ``error``. No partial inserts.
* Idempotency is best-effort: the worker is gated on a
  ``smart_pin_last_fired`` kv marker (see
  :mod:`app.workers.smart_pin_worker`). If two ticks somehow race in
  the same second, the ``UNIQUE(screenshot_id, created_at)`` index
  on the suggestion table fails the duplicate INSERTs cleanly.

The wrapper :class:`app.llm.client._UsageRecordingClient` records a
``llm_usage`` row with ``kind='smart_pin_suggestion'`` so ``/stats/
llm-usage`` shows the cost.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, TypedDict

from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.smart_pin")

Status = Literal[
    "ok",
    "missing_config",
    "no_data",
    "error",
]


class SmartPinResult(TypedDict):
    """Outcome of one :func:`suggest_smart_pins` call."""

    status: Status
    day: str
    candidates: int
    suggested: int


class _Candidate(TypedDict):
    """In-memory snippet handed to the LLM."""

    shot_id: int
    app: str
    window: str
    ocr_excerpt: str


_SYSTEM_PROMPT: str = (
    "You are a memory assistant scanning a day of screenshots. From the "
    "given list of (app, window_title, OCR excerpt) entries, pick up to "
    "{top_n} most likely IMPORTANT moments — code review approvals, "
    "decisions, key events, sign-offs, announcements. Skip idle browsing, "
    "social media, and code in progress. Return STRICT JSON: a JSON array "
    "where each entry is "
    '{{"shot_id": <int>, "reason": "<one short sentence>", "score": <0..1>}}. '
    "No prose before or after the array. If nothing looks important, "
    "return []."
)

#: Trim each OCR excerpt before sending — keeps the prompt cheap and
#: bounded even when a candidate shot contains a wall of text.
_OCR_EXCERPT_CHARS: int = 400

#: Roughly enough room for {top_n} JSON objects with short reasons.
_MAX_TOKENS: int = 500

#: Low-creativity temperature — we want consistent picks, not narrative.
_TEMPERATURE: float = 0.2

#: Hard guard: even if the model ignores the contract, never insert more
#: than this many suggestions in a single call. Defensive cap.
_HARD_TOP_N_CEILING: int = 10


async def suggest_smart_pins(
    day_iso: str,
    max_candidates: int = 30,
    top_n: int = 3,
) -> SmartPinResult:
    """Ask the LLM which of yesterday's shots look IMPORTANT, persist picks.

    Args:
        day_iso: Local-TZ ``YYYY-MM-DD`` day to scan.
        max_candidates: Cap on the candidate list shown to the model.
            Beyond this the prompt gets expensive and the model starts
            to drop items silently. 30 is a good middle ground.
        top_n: Cap on suggestions returned by the model. Hard-clipped
            to :data:`_HARD_TOP_N_CEILING` so a misbehaving model can't
            flood the review UI.

    Returns:
        :class:`SmartPinResult` with ``status``:
          - ``ok`` — call succeeded; ``suggested`` may still be 0 if
            the model returned ``[]`` (nothing looked important).
          - ``missing_config`` — BYO LLM not configured; no rows
            written.
          - ``no_data`` — no eligible shots for ``day_iso``.
          - ``error`` — LLM call raised or returned malformed JSON.
    """
    capped_top_n = max(1, min(int(top_n), _HARD_TOP_N_CEILING))
    capped_max = max(1, int(max_candidates))

    try:
        day = date.fromisoformat(day_iso)
    except ValueError:
        log.warning("smart_pin.bad_day", day=day_iso)
        return {
            "status": "error",
            "day": day_iso,
            "candidates": 0,
            "suggested": 0,
        }

    candidates = await _list_candidates(day=day, max_candidates=capped_max)
    if not candidates:
        log.info("smart_pin.no_data", day=day_iso)
        return {
            "status": "no_data",
            "day": day_iso,
            "candidates": 0,
            "suggested": 0,
        }

    try:
        client = make_client(kind="smart_pin_suggestion")
    except LLMNotConfigured:
        log.info("smart_pin.missing_config", day=day_iso)
        return {
            "status": "missing_config",
            "day": day_iso,
            "candidates": len(candidates),
            "suggested": 0,
        }

    request = CompletionRequest(
        system=_SYSTEM_PROMPT.format(top_n=capped_top_n),
        user=_build_user_prompt(candidates, top_n=capped_top_n),
        max_tokens=_MAX_TOKENS,
        temperature=_TEMPERATURE,
    )

    log.info(
        "smart_pin.generate.start",
        day=day_iso,
        provider=client.provider,
        candidates=len(candidates),
        top_n=capped_top_n,
    )

    try:
        raw = (await client.complete(request)).strip()
    except Exception as exc:
        log.warning("smart_pin.generate.failed", day=day_iso, error=str(exc))
        return {
            "status": "error",
            "day": day_iso,
            "candidates": len(candidates),
            "suggested": 0,
        }

    picks = _parse_llm_picks(raw, candidate_ids={c["shot_id"] for c in candidates})
    if picks is None:
        log.warning("smart_pin.generate.malformed", day=day_iso, raw=raw[:200])
        return {
            "status": "error",
            "day": day_iso,
            "candidates": len(candidates),
            "suggested": 0,
        }

    suggested = await _persist_picks(picks[:capped_top_n])

    log.info(
        "smart_pin.generate.done",
        day=day_iso,
        candidates=len(candidates),
        suggested=suggested,
    )
    return {
        "status": "ok",
        "day": day_iso,
        "candidates": len(candidates),
        "suggested": suggested,
    }


async def _list_candidates(
    *,
    day: date,
    max_candidates: int,
) -> list[_Candidate]:
    """Pick a phash-diverse sample of un-pinned shots from ``day``.

    Strategy: collapse the day's shots to one row per (app, window)
    pair using ``MIN(id)``, then keep up to ``max_candidates`` of the
    newest such rows. SQLite's ``DISTINCT`` is rewritten to a
    ``GROUP BY`` so the OCR/window fields come back together.
    """
    since = datetime.combine(day, time.min, tzinfo=UTC)
    until = since + timedelta(days=1)
    since_iso, until_iso = iso(since), iso(until)

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                MIN(s.id) AS shot_id,
                COALESCE(s.app_name, '') AS app,
                COALESCE(s.window_title, '') AS window,
                SUBSTR(COALESCE(s.ocr_text, ''), 1, ?) AS ocr_excerpt
            FROM screenshots AS s
            WHERE s.captured_at >= ?
              AND s.captured_at < ?
              AND s.tier != 'pinned'
              AND s.ocr_text IS NOT NULL
              AND LENGTH(TRIM(s.ocr_text)) > 0
            GROUP BY COALESCE(s.app_name, ''), COALESCE(s.window_title, '')
            ORDER BY shot_id DESC
            LIMIT ?
            """,
            (_OCR_EXCERPT_CHARS, since_iso, until_iso, max_candidates),
        )
        rows = await cursor.fetchall()

    result: list[_Candidate] = []
    for row in rows:
        shot_id = int(row["shot_id"])
        result.append(
            {
                "shot_id": shot_id,
                "app": str(row["app"] or "").strip(),
                "window": str(row["window"] or "").strip(),
                "ocr_excerpt": str(row["ocr_excerpt"] or "").strip(),
            }
        )
    return result


def _build_user_prompt(candidates: list[_Candidate], *, top_n: int) -> str:
    """Render the candidate list as a JSON block the LLM can reason over."""
    payload = [
        {
            "shot_id": c["shot_id"],
            "app": c["app"],
            "window": c["window"],
            "ocr": c["ocr_excerpt"],
        }
        for c in candidates
    ]
    parts: list[str] = [
        f"Screenshots from yesterday ({len(candidates)} entries). "
        f"Pick up to {top_n} most likely IMPORTANT.",
        "",
        json.dumps(payload, ensure_ascii=False, indent=2),
        "",
        "Reply with the JSON array now.",
    ]
    return "\n".join(parts)


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing ``` fence some models add around JSON."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    first_nl = text.find("\n")
    if first_nl != -1:
        text = text[first_nl + 1 :]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _clamp_score(score_raw: object) -> float:
    """Coerce + clamp the LLM's reported confidence into ``[0.0, 1.0]``."""
    try:
        score = float(score_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # Model occasionally returns ints like "85" (percent) instead of 0.85.
    if score > 1.0:
        score = score / 100.0 if score <= 100.0 else 1.0
    score = max(score, 0.0)
    return min(score, 1.0)


def _parse_one_pick(
    entry: object,
    *,
    candidate_ids: set[int],
) -> tuple[int, str, float] | None:
    """Validate a single ``{shot_id, reason, score}`` JSON entry."""
    if not isinstance(entry, dict):
        return None
    try:
        shot_id = int(entry.get("shot_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if shot_id not in candidate_ids:
        return None
    reason = str(entry.get("reason") or "").strip()
    if not reason:
        return None
    score = _clamp_score(entry.get("score", 0.0))
    return shot_id, reason, score


def _parse_llm_picks(
    raw: str,
    *,
    candidate_ids: set[int],
) -> list[tuple[int, str, float]] | None:
    """Parse the model's JSON reply into a validated pick list.

    Returns ``None`` when the reply is not parseable as a JSON array
    of objects with the required shape. Picks referring to shot_ids
    the model invented (not in ``candidate_ids``) are silently dropped
    rather than failing the whole batch.
    """
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, list):
        return None

    out: list[tuple[int, str, float]] = []
    for entry in data:
        parsed = _parse_one_pick(entry, candidate_ids=candidate_ids)
        if parsed is not None:
            out.append(parsed)
    return out


async def _persist_picks(picks: list[tuple[int, str, float]]) -> int:
    """INSERT each pick. Count rows actually written.

    Duplicates rejected by ``UNIQUE(screenshot_id, created_at)`` are
    swallowed silently — re-running the worker for the same day must
    not crash the loop.
    """
    if not picks:
        return 0

    written = 0
    async with get_connection() as conn:
        for shot_id, reason, score in picks:
            try:
                await conn.execute(
                    """
                    INSERT INTO smart_pin_suggestion
                        (screenshot_id, reason, score)
                    VALUES (?, ?, ?)
                    """,
                    (shot_id, reason, score),
                )
                written += 1
            except Exception as exc:
                log.info(
                    "smart_pin.insert_skipped",
                    shot_id=shot_id,
                    error=str(exc),
                )
        await conn.commit()
    return written


__all__ = ["SmartPinResult", "Status", "suggest_smart_pins"]
