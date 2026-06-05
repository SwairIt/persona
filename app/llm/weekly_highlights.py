"""LLM weekly highlights (v1.46).

Complements :mod:`app.llm.weekly_rollup` — that module produces a
single narrative paragraph per week; this one produces a *curated list*
of 5-7 standout moments. Each pick references one specific artefact
(a screenshot, a screenshot note, or a capture session) and carries a
one-sentence ``reason`` explaining why the LLM thought it was worth
surfacing.

Design rules mirror :mod:`app.llm.weekly_rollup`:

* **Never invent facts** — the system prompt is explicit and the user
  body contains only IDs + short excerpts already on disk.
* **Never crash on misconfiguration** — :class:`LLMNotConfigured` is
  caught and surfaced as ``missing_config`` so the worker just sleeps.
* **Idempotent on the storage side** — if any picks for the target
  week already exist we return ``already_done`` without re-spending
  tokens. The ``UNIQUE(week_start, rank)`` constraint would otherwise
  raise on the second run.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, TypedDict

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

log = get_logger("persona.weekly_highlights")

Status = Literal[
    "ok",
    "already_done",
    "missing_config",
    "no_data",
    "error",
]


class HighlightResult(TypedDict):
    """Outcome of one :func:`generate_highlights` call."""

    status: Status
    week_start: str
    picks_count: int


_SYSTEM_PROMPT: str = (
    "You are a memory assistant. From the structured log of one user "
    "week below pick the 5 to 7 most interesting standout moments — "
    "concrete shots, notes or sessions that a future reader would want "
    "to revisit. For each pick output a JSON object with these keys: "
    "source_kind (one of 'shot', 'note', 'session'), source_id (the "
    "integer id shown in brackets in the input), title (a short label, "
    "max 80 chars), reason (one sentence, max 200 chars, explaining why "
    "this moment stands out). Reply with ONLY a JSON array of those "
    "objects in importance order, most interesting first. No prose, no "
    "markdown fences, no commentary. Do not invent ids that are not in "
    "the input. Reply in the user language."
)

#: Generous cap for the JSON array — 7 picks * ~300 chars each plus
#: brackets/commas comfortably fits.
_MAX_TOKENS: int = 1200

#: Low-creativity temperature — the picks are grounded in the input.
_TEMPERATURE: float = 0.4

#: Hard minimum/maximum picks the LLM is asked to emit. The parser
#: trims silently if the model over-produces.
_MIN_PICKS: int = 5
_MAX_PICKS: int = 7

#: Caps on how much of each source list we feed in. Picked to keep
#: the prompt well under typical 8k-token context windows even when
#: every shot has an OCR snippet.
_MAX_SHOTS: int = 60
_MAX_NOTES: int = 40
_MAX_SESSIONS: int = 30

#: Allowed values for ``source_kind`` — kept in sync with the CHECK in
#: migration 126.
_ALLOWED_KINDS: frozenset[str] = frozenset({"shot", "note", "session"})


def _monday_of(when: date) -> date:
    """Return the Monday of the ISO week containing ``when``."""
    return when - timedelta(days=when.weekday())


def _parse_week_start(week_start_iso: str) -> date | None:
    """Best-effort parse of the caller-supplied week_start string."""
    try:
        return date.fromisoformat(week_start_iso.strip())
    except (AttributeError, ValueError):
        return None


async def _gather_shots(
    conn: aiosqlite.Connection,
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    """Return up to :data:`_MAX_SHOTS` pinned shots from the window.

    Pinned shots are the user's own "this mattered" signal — feeding the
    LLM that subset rather than every capture massively raises the
    signal-to-noise ratio of the resulting picks. If there are not
    enough pinned shots we backfill with the most recent shots of any
    tier so a brand-new install still gets highlights.
    """
    cursor = await conn.execute(
        "SELECT id, captured_at, app_name, window_title, ocr_text "
        "FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? AND tier = 'pinned' "
        "ORDER BY captured_at ASC LIMIT ?",
        (start_utc.isoformat(), end_utc.isoformat(), _MAX_SHOTS),
    )
    rows = await cursor.fetchall()
    shots: list[dict[str, Any]] = [
        {
            "id": int(r["id"]),
            "captured_at": str(r["captured_at"]),
            "app_name": str(r["app_name"]) if r["app_name"] is not None else "",
            "window_title": str(r["window_title"]) if r["window_title"] is not None else "",
            "ocr_text": (str(r["ocr_text"]) if r["ocr_text"] is not None else "")[:200],
        }
        for r in rows
    ]
    if len(shots) >= _MIN_PICKS:
        return shots

    # Backfill with recent shots of any tier so first-week users still
    # get a useful pick list. We exclude IDs we already have to avoid
    # duplicates in the prompt.
    have_ids = {s["id"] for s in shots}
    cursor = await conn.execute(
        "SELECT id, captured_at, app_name, window_title, ocr_text "
        "FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "ORDER BY captured_at DESC LIMIT ?",
        (start_utc.isoformat(), end_utc.isoformat(), _MAX_SHOTS),
    )
    rows = await cursor.fetchall()
    for r in rows:
        sid = int(r["id"])
        if sid in have_ids:
            continue
        shots.append(
            {
                "id": sid,
                "captured_at": str(r["captured_at"]),
                "app_name": str(r["app_name"]) if r["app_name"] is not None else "",
                "window_title": str(r["window_title"]) if r["window_title"] is not None else "",
                "ocr_text": (str(r["ocr_text"]) if r["ocr_text"] is not None else "")[:200],
            }
        )
        if len(shots) >= _MAX_SHOTS:
            break
    return shots


async def _gather_notes(
    conn: aiosqlite.Connection,
    *,
    start_iso: str,
    end_iso: str,
) -> list[dict[str, Any]]:
    """Return up to :data:`_MAX_NOTES` notes whose updated_at is in window.

    The note's ``screenshot_id`` doubles as its primary key (see
    ``002_notes.sql``) — that becomes the ``source_id`` we hand to the
    LLM, so picks can be JOINed back to the captioned shot in the UI.
    """
    cursor = await conn.execute(
        "SELECT screenshot_id, body, updated_at FROM screenshot_notes "
        "WHERE updated_at >= ? AND updated_at < ? "
        "ORDER BY updated_at ASC LIMIT ?",
        (start_iso, end_iso, _MAX_NOTES),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(r["screenshot_id"]),
            "updated_at": str(r["updated_at"]),
            "body": (str(r["body"]) if r["body"] is not None else "").strip()[:400],
        }
        for r in rows
    ]


async def _gather_sessions(
    conn: aiosqlite.Connection,
    *,
    start_iso: str,
    end_iso: str,
) -> list[dict[str, Any]]:
    """Return up to :data:`_MAX_SESSIONS` capture sessions in the window."""
    cursor = await conn.execute(
        "SELECT id, started_at, ended_at, duration_seconds, dominant_app, "
        "screen_count, voice_seconds "
        "FROM capture_session "
        "WHERE started_at >= ? AND started_at < ? "
        "ORDER BY started_at ASC LIMIT ?",
        (start_iso, end_iso, _MAX_SESSIONS),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(r["id"]),
            "started_at": str(r["started_at"]),
            "ended_at": str(r["ended_at"]),
            "duration_seconds": int(r["duration_seconds"] or 0),
            "dominant_app": str(r["dominant_app"]) if r["dominant_app"] is not None else "",
            "screen_count": int(r["screen_count"] or 0),
            "voice_seconds": int(r["voice_seconds"] or 0),
        }
        for r in rows
    ]


def _build_user_prompt(
    *,
    week_start: date,
    week_end: date,
    shots: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> str:
    """Render the user-side message — three labelled blocks of IDs."""
    parts: list[str] = [
        f"Week: {week_start.isoformat()} → {week_end.isoformat()}",
        (
            f"Pick {_MIN_PICKS}-{_MAX_PICKS} standout moments from the "
            "lists below. Reply with ONLY a JSON array."
        ),
        "",
    ]
    if shots:
        parts.append("SHOTS (source_kind=shot):")
        for s in shots:
            label_app = s["app_name"] or "?"
            title = (s["window_title"] or "").replace("\n", " ")[:140]
            ocr = (s["ocr_text"] or "").replace("\n", " ")
            parts.append(
                f"[{s['id']}] {s['captured_at']} [{label_app}] {title} :: {ocr}"
            )
        parts.append("")
    if notes:
        parts.append("NOTES (source_kind=note):")
        for n in notes:
            body = (n["body"] or "").replace("\n", " ")
            parts.append(f"[{n['id']}] {n['updated_at']} :: {body}")
        parts.append("")
    if sessions:
        parts.append("SESSIONS (source_kind=session):")
        for sess in sessions:
            parts.append(
                f"[{sess['id']}] {sess['started_at']} → {sess['ended_at']} "
                f"[{sess['dominant_app'] or '?'}] "
                f"{sess['screen_count']} shots · "
                f"{sess['duration_seconds']}s · "
                f"{sess['voice_seconds']}s voice"
            )
        parts.append("")
    return "\n".join(parts)


def _coerce_pick(raw: Any, *, allowed_ids_by_kind: dict[str, set[int]]) -> dict[str, Any] | None:
    """Return a normalised pick dict or ``None`` if invalid.

    Validates ``source_kind`` is allowed, ``source_id`` parses as int
    and exists in the input we fed the LLM, and ``title``/``reason``
    are non-empty strings. The flat ``if invalid: return None`` chain
    is collapsed into a single guard to stay inside the project's
    PLR0911 return-count budget.
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("source_kind")
    title = raw.get("title")
    reason = raw.get("reason")
    try:
        source_id = int(raw.get("source_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    valid = (
        isinstance(kind, str)
        and kind in _ALLOWED_KINDS
        and source_id in allowed_ids_by_kind.get(kind, set())
        and isinstance(title, str)
        and bool(title.strip())
        and isinstance(reason, str)
        and bool(reason.strip())
    )
    if not valid:
        return None
    # Help the type checker — the predicate above already confirmed
    # both fields are non-empty strings.
    assert isinstance(title, str)
    assert isinstance(reason, str)
    return {
        "source_kind": kind,
        "source_id": source_id,
        "title": title.strip()[:200],
        "reason": reason.strip()[:400],
    }


def _parse_picks(
    raw_text: str,
    *,
    allowed_ids_by_kind: dict[str, set[int]],
) -> list[dict[str, Any]]:
    """Parse ``raw_text`` into a list of validated pick dicts.

    Tolerates Markdown fences the LLM may emit despite the instruction
    not to — strips a leading ``` block prefix before parsing.
    """
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence (with optional language hint).
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
        stripped = stripped.strip()
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    picks: list[dict[str, Any]] = []
    for raw in decoded:
        coerced = _coerce_pick(raw, allowed_ids_by_kind=allowed_ids_by_kind)
        if coerced is not None:
            picks.append(coerced)
        if len(picks) >= _MAX_PICKS:
            break
    return picks


async def _existing_picks(conn: aiosqlite.Connection, week_start_str: str) -> int:
    """Return number of weekly_highlight rows already stored for the week."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS c FROM weekly_highlight WHERE week_start = ?",
        (week_start_str,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["c"])


async def _insert_picks(
    conn: aiosqlite.Connection,
    *,
    week_start_str: str,
    picks: list[dict[str, Any]],
) -> int:
    """Insert validated picks, return count actually inserted."""
    inserted = 0
    for rank, pick in enumerate(picks, start=1):
        await conn.execute(
            "INSERT INTO weekly_highlight "
            "(week_start, rank, source_kind, source_id, title, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                week_start_str,
                rank,
                pick["source_kind"],
                pick["source_id"],
                pick["title"],
                pick["reason"],
            ),
        )
        inserted += 1
    await conn.commit()
    return inserted


async def generate_highlights(week_start_iso: str) -> dict[str, Any]:
    """Generate + persist 5-7 highlight picks for ``week_start_iso``.

    Args:
        week_start_iso: ``YYYY-MM-DD`` of the Monday of the target ISO
            week. Any day inside the week is also accepted — the
            Monday is computed automatically.

    Returns:
        Dict with ``status`` (one of :data:`Status`), ``week_start`` and
        ``picks_count``.
    """
    parsed = _parse_week_start(week_start_iso)
    if parsed is None:
        log.warning("weekly_highlights.bad_week_start", week_start=week_start_iso)
        return {"status": "error", "week_start": week_start_iso, "picks_count": 0}

    week_start = _monday_of(parsed)
    week_end = week_start + timedelta(days=6)
    week_start_str = week_start.isoformat()
    start_utc = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
    end_utc = datetime.combine(
        week_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )

    async with get_connection() as conn:
        already = await _existing_picks(conn, week_start_str)
        if already > 0:
            log.info(
                "weekly_highlights.already_done",
                week_start=week_start_str,
                existing=already,
            )
            return {
                "status": "already_done",
                "week_start": week_start_str,
                "picks_count": already,
            }

        shots = await _gather_shots(conn, start_utc=start_utc, end_utc=end_utc)
        notes = await _gather_notes(
            conn, start_iso=start_utc.isoformat(), end_iso=end_utc.isoformat()
        )
        sessions = await _gather_sessions(
            conn, start_iso=week_start_str, end_iso=(week_end + timedelta(days=1)).isoformat()
        )

        if not shots and not notes and not sessions:
            log.info("weekly_highlights.no_data", week_start=week_start_str)
            return {
                "status": "no_data",
                "week_start": week_start_str,
                "picks_count": 0,
            }

        try:
            client = make_client(kind="weekly_highlights")
        except LLMNotConfigured:
            log.info("weekly_highlights.missing_config", week_start=week_start_str)
            return {
                "status": "missing_config",
                "week_start": week_start_str,
                "picks_count": 0,
            }

        picks = await _request_picks(
            client,
            week_start=week_start,
            week_end=week_end,
            week_start_str=week_start_str,
            shots=shots,
            notes=notes,
            sessions=sessions,
        )
        if not picks:
            return {
                "status": "error",
                "week_start": week_start_str,
                "picks_count": 0,
            }

        inserted = await _insert_picks(
            conn, week_start_str=week_start_str, picks=picks
        )

    log.info(
        "weekly_highlights.generate.done",
        week_start=week_start_str,
        picks=inserted,
    )
    return {
        "status": "ok",
        "week_start": week_start_str,
        "picks_count": inserted,
    }


async def _request_picks(
    client: LLMClient,
    *,
    week_start: date,
    week_end: date,
    week_start_str: str,
    shots: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Call the LLM and return parsed + validated picks (possibly empty)."""
    allowed_ids_by_kind: dict[str, set[int]] = {
        "shot": {s["id"] for s in shots},
        "note": {n["id"] for n in notes},
        "session": {sess["id"] for sess in sessions},
    }
    user_body = _build_user_prompt(
        week_start=week_start,
        week_end=week_end,
        shots=shots,
        notes=notes,
        sessions=sessions,
    )
    request = CompletionRequest(
        system=_SYSTEM_PROMPT,
        user=user_body,
        max_tokens=_MAX_TOKENS,
        temperature=_TEMPERATURE,
    )

    log.info(
        "weekly_highlights.generate.start",
        week_start=week_start_str,
        shots=len(shots),
        notes=len(notes),
        sessions=len(sessions),
        provider=client.provider,
    )

    try:
        raw_text = (await client.complete(request)).strip()
    except Exception as exc:
        log.warning(
            "weekly_highlights.generate.failed",
            week_start=week_start_str,
            error=str(exc),
        )
        return []
    if not raw_text:
        log.warning("weekly_highlights.generate.empty", week_start=week_start_str)
        return []
    picks = _parse_picks(raw_text, allowed_ids_by_kind=allowed_ids_by_kind)
    if not picks:
        log.warning(
            "weekly_highlights.parse.empty",
            week_start=week_start_str,
            response_len=len(raw_text),
        )
    return picks


__all__ = ["HighlightResult", "Status", "generate_highlights"]
