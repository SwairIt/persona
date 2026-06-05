"""AI-suggested daily reminders — what's worth remembering tomorrow.

The user opens ``/reminders/ai`` and sees up to 3 short bullets the LLM
extracted from yesterday's activity. Each bullet is one row in
:func:`app.storage.migrations.131_ai_reminder`. Suggestions are produced
by a single LLM call seeded with three orthogonal signals for the target
day:

* ``hourly_card.summary`` — the per-hour markdown blocks (skimmed to the
  most recent ~12 to keep the prompt small).
* ``daily_pin.pin`` — the ultra-compact one-line recap that survives the
  retention sweep.
* ``notes`` rows whose ``created_at`` falls inside the target day — these
  are the user's own freeform jottings, the strongest "do not forget"
  signal we have.

The model is asked for STRICT JSON (an array of ``{title, body,
severity, due_at}``) so we can parse and validate every field before
storage. Anything that doesn't survive validation is silently dropped —
we'd rather show two clean cards than one weird one. The function never
raises on a malformed LLM reply; the caller (worker / route) gets back
``count = 0`` and a logged warning.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, Literal, TypedDict, cast

from app.llm.client import CompletionRequest, LLMClient, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.ai_reminders")

Status = Literal["ok", "empty", "missing_config", "llm_failed"]
Severity = Literal["info", "warn", "action"]

_VALID_SEVERITIES: Final[frozenset[str]] = frozenset({"info", "warn", "action"})
_MAX_SUGGESTIONS: Final[int] = 3
_MAX_HOURLY_CARDS: Final[int] = 12
_MAX_NOTES: Final[int] = 20
_TITLE_LIMIT: Final[int] = 200
_BODY_LIMIT: Final[int] = 1000


class GenerateResult(TypedDict):
    status: Status
    source_day: str
    count: int


class _Suggestion(TypedDict):
    title: str
    body: str | None
    severity: Severity
    due_at: str | None


_SYSTEM: Final[str] = (
    "You are a thoughtful memory assistant. From this day summary, suggest "
    "UP TO 3 reminders for tomorrow — each is something the user should not "
    "forget. Return strict JSON array of {title, body, severity "
    "(info|warn|action), due_at (ISO|null)}. Empty array if nothing actionable."
)


# The LLM is asked for a bare JSON array but smaller models often wrap it
# in prose or fences. We strip both and fall back to the first ``[ ... ]``
# substring before giving up.
_JSON_ARRAY_RE: Final[re.Pattern[str]] = re.compile(r"\[.*\]", re.DOTALL)
_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^```(?:json)?\s*|\s*```$", re.MULTILINE
)


def _parse_day(day_iso: str) -> date:
    """Validate ``YYYY-MM-DD``; raise ``ValueError`` on bad input."""
    return datetime.strptime(day_iso, "%Y-%m-%d").date()


async def _gather_signal(
    conn: aiosqlite.Connection, day_iso: str
) -> tuple[list[str], str | None, list[str]]:
    """Pull the three orthogonal signals fed to the LLM.

    Returns ``(hourly_summaries, daily_pin, notes_bodies)``. Each list is
    capped so a freakishly busy day cannot blow the prompt budget.
    """
    parsed = _parse_day(day_iso)
    since = datetime.combine(parsed, time.min, tzinfo=UTC)
    until = since + timedelta(days=1)
    since_iso, until_iso = iso(since), iso(until)

    cursor = await conn.execute(
        "SELECT summary FROM hourly_card "
        "WHERE hour_start >= ? AND hour_start < ? "
        "ORDER BY hour_start DESC LIMIT ?",
        (since_iso, until_iso, _MAX_HOURLY_CARDS),
    )
    hourly_rows = await cursor.fetchall()
    hourly_summaries: list[str] = [
        str(row["summary"]) for row in hourly_rows if row["summary"]
    ]

    cursor = await conn.execute(
        "SELECT pin FROM daily_pin WHERE day = ?",
        (day_iso,),
    )
    pin_row = await cursor.fetchone()
    daily_pin: str | None = str(pin_row["pin"]) if pin_row else None

    cursor = await conn.execute(
        "SELECT body FROM notes "
        "WHERE created_at >= ? AND created_at < ? "
        "ORDER BY created_at DESC LIMIT ?",
        (since_iso, until_iso, _MAX_NOTES),
    )
    note_rows = await cursor.fetchall()
    notes_bodies: list[str] = [
        str(row["body"]) for row in note_rows if row["body"]
    ]

    return hourly_summaries, daily_pin, notes_bodies


def _build_user_prompt(
    *,
    day_iso: str,
    hourly_summaries: list[str],
    daily_pin: str | None,
    notes_bodies: list[str],
) -> str:
    """Compose the user-side prompt body fed alongside the system message."""
    pin_block = daily_pin or "(no pin yet)"
    hourly_block = (
        "\n---\n".join(s.strip() for s in hourly_summaries[:_MAX_HOURLY_CARDS])
        if hourly_summaries
        else "(no hourly cards)"
    )
    notes_block = (
        "\n---\n".join(b.strip() for b in notes_bodies[:_MAX_NOTES])
        if notes_bodies
        else "(no notes)"
    )
    return (
        f"Day: {day_iso}\n\n"
        f"Daily pin:\n{pin_block}\n\n"
        f"Hourly summaries (most recent first):\n{hourly_block}\n\n"
        f"Notes from today:\n{notes_block}\n\n"
        "Now produce the JSON array of UP TO 3 reminders for tomorrow."
    )


def _parse_suggestions(text: str) -> list[_Suggestion]:
    """Best-effort decode of the LLM JSON array.

    Returns an empty list when the reply is unparseable, the JSON is not
    an array, or every entry fails validation. Per-entry validation is
    permissive on optional fields (``body``, ``due_at``) but strict on
    the required ones (``title`` non-empty, ``severity`` in the allowed
    set after normalisation).
    """
    if not text:
        return []

    candidates: list[str] = []
    stripped = _FENCE_RE.sub("", text).strip()
    if stripped:
        candidates.append(stripped)
    candidates.extend(_JSON_ARRAY_RE.findall(text))

    for raw in candidates:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        cleaned: list[_Suggestion] = []
        for entry in parsed:
            suggestion = _coerce_entry(entry)
            if suggestion is not None:
                cleaned.append(suggestion)
            if len(cleaned) >= _MAX_SUGGESTIONS:
                break
        return cleaned
    return []


def _coerce_entry(entry: object) -> _Suggestion | None:
    """Validate one decoded JSON object into a stored-shape suggestion."""
    if not isinstance(entry, dict):
        return None

    raw_title = entry.get("title")
    if not isinstance(raw_title, str):
        return None
    title = raw_title.strip()[:_TITLE_LIMIT]
    if not title:
        return None

    raw_body = entry.get("body")
    body: str | None = (
        raw_body.strip()[:_BODY_LIMIT] or None
        if isinstance(raw_body, str)
        else None
    )

    raw_severity = entry.get("severity")
    severity: Severity = "info"
    if isinstance(raw_severity, str):
        candidate = raw_severity.strip().lower()
        if candidate in _VALID_SEVERITIES:
            severity = cast("Severity", candidate)

    raw_due = entry.get("due_at")
    due_at: str | None
    if isinstance(raw_due, str):
        normalised = raw_due.strip()
        try:
            datetime.fromisoformat(normalised.replace("Z", "+00:00"))
        except ValueError:
            due_at = None
        else:
            due_at = normalised
    else:
        due_at = None

    return {
        "title": title,
        "body": body,
        "severity": severity,
        "due_at": due_at,
    }


async def _persist(
    conn: aiosqlite.Connection,
    *,
    day_iso: str,
    suggestions: list[_Suggestion],
) -> int:
    """Insert validated suggestions into ``ai_reminder``.

    Returns the count actually inserted. Each row is written with a
    parametrised SQL statement so user-controlled LLM output cannot
    influence the query shape.
    """
    inserted = 0
    for suggestion in suggestions:
        await conn.execute(
            "INSERT INTO ai_reminder "
            "(source_day, title, body, severity, due_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                day_iso,
                suggestion["title"],
                suggestion["body"],
                suggestion["severity"],
                suggestion["due_at"],
            ),
        )
        inserted += 1
    if inserted:
        await conn.commit()
    return inserted


async def generate_reminders(
    day_iso: str,
    *,
    client: LLMClient | None = None,
) -> GenerateResult:
    """Generate up to 3 reminders for tomorrow using ``day_iso`` as source.

    Args:
        day_iso: Calendar day in ``YYYY-MM-DD`` form (local TZ).
        client: Optional preconstructed LLM client — handy for tests.

    Returns:
        ``GenerateResult`` with one of:
          - ``status="ok"``: at least one row stored (``count`` >= 1).
          - ``status="empty"``: signal collected fine but LLM returned
            no actionable suggestions (``count == 0``).
          - ``status="missing_config"``: BYO LLM not wired; nothing
            persisted.
          - ``status="llm_failed"``: LLM call raised; nothing persisted.
    """
    canonical = _parse_day(day_iso).isoformat()

    async with get_connection() as conn:
        hourly_summaries, daily_pin, notes_bodies = await _gather_signal(
            conn, canonical
        )

    try:
        llm = client or make_client(kind="ai_reminders")
    except LLMNotConfigured:
        log.info("ai_reminders.missing_config", day=canonical)
        return {
            "status": "missing_config",
            "source_day": canonical,
            "count": 0,
        }

    user_message = _build_user_prompt(
        day_iso=canonical,
        hourly_summaries=hourly_summaries,
        daily_pin=daily_pin,
        notes_bodies=notes_bodies,
    )
    request = CompletionRequest(
        system=_SYSTEM,
        user=user_message,
        max_tokens=600,
        temperature=0.4,
    )

    log.info(
        "ai_reminders.generate.start",
        day=canonical,
        hourly_count=len(hourly_summaries),
        notes_count=len(notes_bodies),
        has_pin=daily_pin is not None,
        provider=llm.provider,
    )

    try:
        text = (await llm.complete(request)).strip()
    except Exception as exc:
        log.warning(
            "ai_reminders.llm_failed",
            day=canonical,
            error=str(exc),
        )
        return {"status": "llm_failed", "source_day": canonical, "count": 0}

    suggestions = _parse_suggestions(text)
    if not suggestions:
        log.info("ai_reminders.empty", day=canonical, raw_len=len(text))
        return {"status": "empty", "source_day": canonical, "count": 0}

    async with get_connection() as conn:
        inserted = await _persist(
            conn, day_iso=canonical, suggestions=suggestions
        )

    log.info(
        "ai_reminders.generate.done",
        day=canonical,
        inserted=inserted,
        provider=llm.provider,
    )
    return {"status": "ok", "source_day": canonical, "count": inserted}


__all__ = ["GenerateResult", "Severity", "Status", "generate_reminders"]
