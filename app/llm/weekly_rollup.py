"""LLM weekly rollup (v1.27).

Compresses one ISO week — the seven ``daily_pin`` rows plus the up to
168 ``hourly_card`` rows — into a single first-person narrative
paragraph and persists it on the matching ``weekly_card`` row at
``llm_summary``.

Design rules mirror :mod:`app.llm.card_enricher`:

* **Never invent facts** — the system prompt is explicit and the user
  body contains only data already on disk.
* **Never crash on misconfiguration** — :class:`LLMNotConfigured` is
  caught and surfaced as ``missing_config``. The worker keeps looping.
* **Idempotent** — a second call for a row whose ``llm_summary`` is
  already non-NULL returns ``already_done`` without writing.

The wrapper :class:`_UsageRecordingClient` from :mod:`app.llm.client`
records a ``llm_usage`` row with ``kind='weekly_rollup'`` on every
call so the operator can see the cost on ``/stats/llm-usage``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    import aiosqlite

from app.llm.client import (
    CompletionRequest,
    LLMClient,
    LLMNotConfigured,
    make_client,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.weekly_rollup")

Status = Literal[
    "ok",
    "already_done",
    "missing_config",
    "no_data",
    "error",
]


class RollupResult(TypedDict):
    """Outcome of one :func:`rollup_week` call."""

    status: Status
    week_start: str


_SYSTEM_PROMPT: str = (
    "You are a memory assistant. Write a single-paragraph narrative "
    "summary of the user week from these structured rollups. Do not "
    "invent. Reply in the user language."
)

#: Cap on the narrative the LLM is allowed to emit. 400 tokens
#: comfortably fits one rich paragraph in either Latin or Cyrillic
#: scripts while keeping the per-call cost bounded.
_MAX_TOKENS: int = 400

#: Low-creativity temperature — the narrative is grounded in the
#: structured rollups and we explicitly forbid invention.
_TEMPERATURE: float = 0.3

#: Cap on how many hourly card summary lines we feed in. 168 fits a
#: full week (7 * 24) but a sparse week may have fewer; truncating
#: defensively guards against a future migration that buckets sub-hour.
_MAX_HOURLY_LINES: int = 200


def _monday_of(when: date) -> date:
    """Return the Monday of the ISO week containing ``when``."""
    return when - timedelta(days=when.weekday())


def _parse_week_start(week_start_iso: str) -> date | None:
    """Best-effort parse of the caller-supplied week_start string."""
    try:
        return date.fromisoformat(week_start_iso.strip())
    except (AttributeError, ValueError):
        return None


def _first_summary_line(summary: str) -> str:
    """Return the first non-blank line of an hourly_card summary."""
    for raw in summary.splitlines():
        stripped = raw.strip()
        if stripped:
            return stripped
    return ""


def _build_user_prompt(
    *,
    week_start: date,
    week_end: date,
    daily_pins: list[dict[str, str]],
    hourly_lines: list[str],
) -> str:
    """Render the user-side message for the LLM call.

    The body is two clearly labelled blocks: the seven daily pins and
    the condensed first-line-of-summary list from the hourly cards.
    """
    parts: list[str] = [
        f"Week: {week_start.isoformat()} → {week_end.isoformat()}",
        "",
    ]
    if daily_pins:
        parts.append("DAILY PINS:")
        for pin in daily_pins:
            parts.append(f"- {pin['day']}: {pin['pin']}")
        parts.append("")
    if hourly_lines:
        parts.append("HOURLY CARDS (condensed, one line per hour):")
        parts.extend(f"- {line}" for line in hourly_lines)
        parts.append("")
    parts.append("Write the narrative paragraph now.")
    return "\n".join(parts)


async def _gather_inputs(
    conn: aiosqlite.Connection,
    *,
    week_start: date,
    week_end: date,
) -> tuple[list[dict[str, str]], list[str]]:
    """Return the daily pins + condensed hourly lines for the window."""
    week_start_str = week_start.isoformat()
    week_end_str = week_end.isoformat()

    cursor = await conn.execute(
        "SELECT day, pin FROM daily_pin "
        "WHERE day >= ? AND day <= ? "
        "ORDER BY day ASC",
        (week_start_str, week_end_str),
    )
    pin_rows = await cursor.fetchall()
    daily_pins: list[dict[str, str]] = [
        {"day": str(r["day"]), "pin": str(r["pin"] or "").strip()}
        for r in pin_rows
        if str(r["pin"] or "").strip()
    ]

    start_utc = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
    end_utc = datetime.combine(
        week_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )
    cursor = await conn.execute(
        "SELECT hour_start, summary FROM hourly_card "
        "WHERE hour_start >= ? AND hour_start < ? "
        "ORDER BY hour_start ASC LIMIT ?",
        (start_utc.isoformat(), end_utc.isoformat(), _MAX_HOURLY_LINES),
    )
    hourly_rows = await cursor.fetchall()
    hourly_lines: list[str] = []
    for r in hourly_rows:
        line = _first_summary_line(str(r["summary"] or ""))
        if line:
            hourly_lines.append(f"{r['hour_start']}: {line}")

    return daily_pins, hourly_lines


async def rollup_week(week_start_iso: str) -> dict[str, str]:
    """Generate + persist a weekly LLM rollup for ``week_start_iso``.

    Args:
        week_start_iso: ``YYYY-MM-DD`` of the Monday of the target
            ISO week. Any day inside the week is also accepted —
            the Monday is computed automatically.

    Returns:
        Dict with ``status`` (one of :data:`Status`) + ``week_start``.
    """
    parsed = _parse_week_start(week_start_iso)
    if parsed is None:
        log.warning("weekly_rollup.bad_week_start", week_start=week_start_iso)
        return {"status": "error", "week_start": week_start_iso}

    week_start = _monday_of(parsed)
    week_end = week_start + timedelta(days=6)
    week_start_str = week_start.isoformat()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT llm_summary FROM weekly_card WHERE week_start = ?",
            (week_start_str,),
        )
        card_row = await cursor.fetchone()
        if card_row is None:
            log.info("weekly_rollup.no_card", week_start=week_start_str)
            return {"status": "no_data", "week_start": week_start_str}

        if card_row["llm_summary"] is not None and str(
            card_row["llm_summary"]
        ).strip():
            log.info("weekly_rollup.already_done", week_start=week_start_str)
            return {"status": "already_done", "week_start": week_start_str}

        narrative, status = await _generate_narrative(
            conn,
            week_start=week_start,
            week_end=week_end,
            week_start_str=week_start_str,
        )
        if status != "ok":
            return {"status": status, "week_start": week_start_str}

        generated_at = datetime.now(tz=UTC).isoformat()
        await conn.execute(
            "UPDATE weekly_card "
            "SET llm_summary = ?, llm_generated_at = ? "
            "WHERE week_start = ?",
            (narrative, generated_at, week_start_str),
        )
        await conn.commit()

    log.info(
        "weekly_rollup.generate.done",
        week_start=week_start_str,
        chars=len(narrative),
    )
    return {"status": "ok", "week_start": week_start_str}


async def _generate_narrative(
    conn: aiosqlite.Connection,
    *,
    week_start: date,
    week_end: date,
    week_start_str: str,
) -> tuple[str, Status]:
    """Gather inputs, call the LLM, return ``(text, status)``.

    Splitting this out of :func:`rollup_week` keeps the parent body
    inside the project's PLR0911 return-count budget while letting
    each non-``ok`` exit log its own structured event.
    """
    daily_pins, hourly_lines = await _gather_inputs(
        conn, week_start=week_start, week_end=week_end
    )

    if not daily_pins and not hourly_lines:
        log.info("weekly_rollup.no_data", week_start=week_start_str)
        return "", "no_data"

    try:
        client = make_client(kind="weekly_rollup")
    except LLMNotConfigured:
        log.info("weekly_rollup.missing_config", week_start=week_start_str)
        return "", "missing_config"

    user_body = _build_user_prompt(
        week_start=week_start,
        week_end=week_end,
        daily_pins=daily_pins,
        hourly_lines=hourly_lines,
    )
    request = CompletionRequest(
        system=_SYSTEM_PROMPT,
        user=user_body,
        max_tokens=_MAX_TOKENS,
        temperature=_TEMPERATURE,
    )

    log.info(
        "weekly_rollup.generate.start",
        week_start=week_start_str,
        pins=len(daily_pins),
        hours=len(hourly_lines),
        provider=client.provider,
    )

    narrative = await _safe_complete(client, request, week_start_str)
    if not narrative:
        return "", "error"
    return narrative, "ok"


async def _safe_complete(
    client: LLMClient,
    request: CompletionRequest,
    week_start_str: str,
) -> str:
    """Call the LLM and return ``""`` on failure or empty output.

    Folding the two error paths into a single helper keeps the parent
    :func:`rollup_week` body inside the project's PLR0911 return-count
    budget without losing structured logging for either case.
    """
    try:
        narrative = (await client.complete(request)).strip()
    except Exception as exc:
        log.warning(
            "weekly_rollup.generate.failed",
            week_start=week_start_str,
            error=str(exc),
        )
        return ""
    if not narrative:
        log.warning(
            "weekly_rollup.generate.empty",
            week_start=week_start_str,
        )
        return ""
    return narrative


__all__ = ["RollupResult", "Status", "rollup_week"]
