"""Per-day Markdown export — one comprehensive ``.md`` journal per day.

Composes a Markdown document that bundles every Persona surface for a
single local calendar day into a file that opens cleanly in Obsidian,
renders on GitHub, and round-trips through Pandoc without surprises.

Pieces folded in (each section is skipped when its source is empty):

* H1 title with the ISO date.
* TL;DR from the ``daily_digest`` row (the same body the journal
  export uses).
* Daily pin one-liner from ``daily_pin``; if a v1.40 LLM narrative
  enrichment is present (``daily_pin.llm_narrative``) it is rendered
  below the heuristic one-liner as a narrative paragraph.
* Hourly cards table (``hour | apps | screens | voice | top_words``)
  pulled from ``hourly_card`` joined with screen counts so the
  numbers match what the dashboard shows.
* Standalone notes created that day (from ``notes``) — markdown body
  kept verbatim apart from a soft cap to protect huge clips.
* Pinned shots (``screenshots.tier = 'pinned'``) rendered as one
  bullet per shot with a permalink-style ``[shot #ID](/shot/ID)`` so
  the user can jump straight back into Persona.
* Reactions summary — ``shot_reaction`` rows grouped by emoji with
  total count + link to the most recent reacted shot of the day.
* Long reads — ``long_read`` sessions that *started* that day, with
  duration and window title.
* Top OCR keywords for the whole day, deduplicated across the day's
  hourly cards (drops stop-words via the hourly_card heuristic, which
  has already filtered them once).

Wikilinks for known entities mirror :mod:`app.obsidian_sync`: capitalised
names from the ``entity`` table are wrapped in ``[[Name]]`` whenever
they appear in the rendered body, longest-first so multi-word entities
beat single-word prefixes. Existing wikilinks are left alone.

The function is a pure read against SQLite — no LLM calls, no network.
A missing table is treated as "no data for that section" so older
Persona installs that pre-date one of the dependency migrations still
produce a valid (if shorter) document.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from app.logging_setup import get_logger
from app.obsidian_sync import (
    _format_clock,
    _format_hour_label,
    _parse_apps_json,
    _wikilink_entities,
)
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.day_markdown")


# Soft caps so a runaway capture day cannot produce a 50 MB markdown
# file. Each ceiling is comfortably above the count an active day would
# produce — see the same constants in :mod:`app.obsidian_sync` for the
# rationale.
_MAX_NOTE_BODY_CHARS: Final[int] = 4_000
_MAX_PINNED_SHOTS_PER_DAY: Final[int] = 60
_MAX_LONG_READS_PER_DAY: Final[int] = 30
_MAX_TOP_KEYWORDS: Final[int] = 25


class _ReactionSummary(dict[str, Any]):
    """Typed alias for the per-emoji reaction summary row.

    A subclass of ``dict`` so the route layer can serialise it directly.
    Keys: ``emoji``, ``count``, ``last_shot_id``.
    """


# ---------------------------------------------------------------------------
# Day-boundary helpers (local-tz, mirroring app.web.routes.day_json)
# ---------------------------------------------------------------------------


def _parse_day(day_iso: str) -> date:
    """Strictly parse ``YYYY-MM-DD``; caller raises HTTP 400 on ``ValueError``."""
    return date.fromisoformat(day_iso.strip())


def _day_bounds_utc(day: date) -> tuple[str, str]:
    """Return ISO UTC ``[start, end_inclusive]`` strings for one day.

    The closed upper bound mirrors :mod:`app.obsidian_sync` so the two
    surfaces agree on which ``captured_at`` rows belong to a given day.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Row fetchers — every fetcher returns a small typed dict and swallows
# ``sqlite3.OperationalError`` so a missing table on an old install just
# means "skip this section".
# ---------------------------------------------------------------------------


async def _fetch_daily_digest(
    conn: aiosqlite.Connection, day_iso: str
) -> str | None:
    try:
        cursor = await conn.execute(
            "SELECT body FROM daily_digest WHERE day = ?",
            (day_iso,),
        )
        row = await cursor.fetchone()
    except sqlite3.OperationalError as exc:
        log.debug("day_markdown.fetch.digest.skipped", error=str(exc))
        return None
    if row is None:
        return None
    body = row["body"]
    if body is None:
        return None
    text = str(body).strip()
    return text or None


async def _fetch_daily_pin_row(
    conn: aiosqlite.Connection, day_iso: str
) -> tuple[Any | None, bool]:
    """Return ``(row, enriched_query_ok)`` for the daily-pin lookup.

    Tries the enriched ``SELECT pin, llm_narrative`` first (migration
    113+); falls back to the legacy ``SELECT pin`` form when the
    column is absent. The second tuple element tells the caller
    whether ``row['llm_narrative']`` is safe to read.
    """
    try:
        cursor = await conn.execute(
            "SELECT pin, llm_narrative FROM daily_pin WHERE day = ?",
            (day_iso,),
        )
        return await cursor.fetchone(), True
    except sqlite3.OperationalError:
        pass
    try:
        cursor = await conn.execute(
            "SELECT pin FROM daily_pin WHERE day = ?",
            (day_iso,),
        )
        return await cursor.fetchone(), False
    except sqlite3.OperationalError as exc:
        log.debug("day_markdown.fetch.pin.skipped", error=str(exc))
        return None, False


async def _fetch_daily_pin(
    conn: aiosqlite.Connection, day_iso: str
) -> dict[str, str | None] | None:
    """Return ``{"pin": str, "narrative": str | None}`` or ``None``.

    Splits the SQL fetch into a separate helper so this function stays
    under the ``ruff PLR0911`` return-count ceiling — every guard that
    drops out to ``None`` is part of one linear decision tree rather
    than an exception fallback.
    """
    row, enriched = await _fetch_daily_pin_row(conn, day_iso)
    if row is None:
        return None
    pin = row["pin"]
    if pin is None:
        return None
    text = str(pin).strip()
    if not text:
        return None
    narrative: str | None = None
    if enriched:
        raw_narrative = row["llm_narrative"]
        if raw_narrative is not None:
            narrative = str(raw_narrative).strip() or None
    return {"pin": text, "narrative": narrative}


async def _fetch_hourly_cards(
    conn: aiosqlite.Connection, day: date
) -> list[dict[str, Any]]:
    start_iso, end_iso = _day_bounds_utc(day)
    try:
        cursor = await conn.execute(
            "SELECT hour_start, summary, apps_json, screen_count, "
            "       audio_seconds, top_words, transcript_excerpt "
            "FROM hourly_card "
            "WHERE hour_start >= ? AND hour_start <= ? "
            "ORDER BY hour_start ASC",
            (start_iso, end_iso),
        )
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("day_markdown.fetch.hourly.skipped", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "hour_start": str(row["hour_start"]),
                "summary": str(row["summary"] or "").strip(),
                "apps_json": str(row["apps_json"] or "").strip(),
                "screen_count": int(row["screen_count"] or 0),
                "audio_seconds": int(row["audio_seconds"] or 0),
                "top_words": str(row["top_words"] or "").strip(),
                "transcript_excerpt": str(
                    row["transcript_excerpt"] or ""
                ).strip(),
            }
        )
    return out


async def _fetch_notes(
    conn: aiosqlite.Connection, day: date
) -> list[dict[str, Any]]:
    """Standalone ``notes`` rows created during ``day`` (UTC).

    Encrypted notes are surfaced with a ``[locked]`` marker so the
    reader knows a note existed without ever seeing the ciphertext.
    """
    start_iso, end_iso = _day_bounds_utc(day)
    try:
        cursor = await conn.execute(
            "SELECT id, title, body, source, encrypted, created_at "
            "FROM notes "
            "WHERE created_at >= ? AND created_at <= ? "
            "ORDER BY created_at ASC",
            (start_iso, end_iso),
        )
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("day_markdown.fetch.notes.skipped", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        is_encrypted = bool(int(row["encrypted"] or 0))
        body_raw = "" if is_encrypted else str(row["body"] or "").strip()
        body = body_raw[:_MAX_NOTE_BODY_CHARS]
        truncated = len(body_raw) > _MAX_NOTE_BODY_CHARS
        out.append(
            {
                "id": int(row["id"]),
                "title": str(row["title"] or "").strip(),
                "body": body,
                "truncated": truncated,
                "source": str(row["source"] or "").strip(),
                "encrypted": is_encrypted,
                "created_at": str(row["created_at"]),
            }
        )
    return out


async def _fetch_pinned_shots(
    conn: aiosqlite.Connection, day: date
) -> list[dict[str, Any]]:
    start_iso, end_iso = _day_bounds_utc(day)
    try:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, window_title "
            "FROM screenshots "
            "WHERE tier = 'pinned' "
            "  AND captured_at >= ? AND captured_at <= ? "
            "ORDER BY captured_at ASC "
            "LIMIT ?",
            (start_iso, end_iso, _MAX_PINNED_SHOTS_PER_DAY),
        )
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("day_markdown.fetch.pinned.skipped", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "captured_at": str(row["captured_at"]),
                "app_name": str(row["app_name"] or "").strip(),
                "window_title": str(row["window_title"] or "").strip(),
            }
        )
    return out


async def _fetch_reactions(
    conn: aiosqlite.Connection, day: date
) -> list[dict[str, Any]]:
    """Per-emoji reaction summary scoped to shots captured that day."""
    start_iso, end_iso = _day_bounds_utc(day)
    try:
        cursor = await conn.execute(
            "SELECT r.emoji AS emoji, "
            "       COUNT(*) AS n, "
            "       MAX(r.screenshot_id) AS last_shot_id "
            "FROM shot_reaction r "
            "JOIN screenshots s ON s.id = r.screenshot_id "
            "WHERE s.captured_at >= ? AND s.captured_at <= ? "
            "GROUP BY r.emoji "
            "ORDER BY n DESC, r.emoji ASC",
            (start_iso, end_iso),
        )
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("day_markdown.fetch.reactions.skipped", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "emoji": str(row["emoji"] or ""),
                "count": int(row["n"] or 0),
                "last_shot_id": int(row["last_shot_id"] or 0),
            }
        )
    return out


async def _fetch_long_reads(
    conn: aiosqlite.Connection, day: date
) -> list[dict[str, Any]]:
    start_iso, end_iso = _day_bounds_utc(day)
    try:
        cursor = await conn.execute(
            "SELECT id, window_title, app_name, started_at, ended_at, "
            "       duration_seconds, screenshot_id_first "
            "FROM long_read "
            "WHERE started_at >= ? AND started_at <= ? "
            "ORDER BY started_at ASC "
            "LIMIT ?",
            (start_iso, end_iso, _MAX_LONG_READS_PER_DAY),
        )
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("day_markdown.fetch.long_reads.skipped", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        first_shot_raw = row["screenshot_id_first"]
        out.append(
            {
                "id": int(row["id"]),
                "window_title": str(row["window_title"] or "").strip(),
                "app_name": str(row["app_name"] or "").strip(),
                "started_at": str(row["started_at"]),
                "ended_at": str(row["ended_at"]),
                "duration_seconds": int(row["duration_seconds"] or 0),
                "first_shot_id": (
                    int(first_shot_raw) if first_shot_raw is not None else None
                ),
            }
        )
    return out


async def _fetch_known_entities(conn: aiosqlite.Connection) -> list[str]:
    """Return every known entity name sorted longest-first.

    Mirrors :func:`app.obsidian_sync._fetch_known_entities` — see that
    helper for the rationale (long-prefix-wins, missing table tolerated).
    """
    try:
        cursor = await conn.execute("SELECT name FROM entity")
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("day_markdown.fetch.entities.skipped", error=str(exc))
        return []
    names: list[str] = []
    for row in rows:
        raw = str(row["name"] or "").strip()
        if len(raw) >= 2:
            names.append(raw)
    names.sort(key=len, reverse=True)
    return names


# ---------------------------------------------------------------------------
# Derived aggregates
# ---------------------------------------------------------------------------


def _top_keywords_across_hours(hourly: list[dict[str, Any]]) -> list[str]:
    """Merge per-hour ``top_words`` strings into a single ordered list.

    The hourly_card already filtered stop-words; we just need to keep
    the first-seen order so words that appear in many hours rank above
    one-off hits, and de-duplicate.
    """
    seen: dict[str, int] = {}
    for card in hourly:
        words = [w.strip() for w in card["top_words"].split(",") if w.strip()]
        for word in words:
            key = word.lower()
            seen[key] = seen.get(key, 0) + 1
    if not seen:
        return []
    ordered = sorted(seen.items(), key=lambda pair: (-pair[1], pair[0]))
    return [w for w, _ in ordered[:_MAX_TOP_KEYWORDS]]


def _format_duration(seconds: int) -> str:
    """Render a duration in seconds as ``Hh MMm`` or ``Mm``."""
    if seconds <= 0:
        return "0m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining == 0:
        return f"{hours}h"
    return f"{hours}h {remaining:02d}m"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _h(level: int, text: str) -> str:
    return ("#" * level) + " " + text


def _render_header(day_iso: str) -> list[str]:
    return [
        f"# Day journal — {day_iso}",
        "",
        (
            "Generated by Persona. Markdown is valid for Obsidian, "
            "GitHub and Pandoc."
        ),
        "",
    ]


def _render_tldr(body: str | None, entities: list[str]) -> list[str]:
    out: list[str] = [_h(2, "TL;DR")]
    if body:
        out.append(_wikilink_entities(body, entities))
    else:
        out.append("_No digest for this day._")
    out.append("")
    return out


def _render_daily_pin(
    pin_payload: dict[str, str | None] | None, entities: list[str]
) -> list[str]:
    out: list[str] = [_h(2, "Daily pin")]
    if pin_payload is None:
        out.append("_No pin for this day._")
        out.append("")
        return out
    pin = pin_payload["pin"] or ""
    narrative = pin_payload["narrative"]
    out.append(_wikilink_entities(pin, entities))
    if narrative:
        out.append("")
        out.append(_wikilink_entities(narrative, entities))
    out.append("")
    return out


def _render_hourly_table(cards: list[dict[str, Any]]) -> list[str]:
    out: list[str] = [_h(2, "Hourly cards")]
    if not cards:
        out.append("_No hourly cards for this day._")
        out.append("")
        return out
    out.append("| Hour | Apps | Screens | Voice | Top words |")
    out.append("| --- | --- | ---: | ---: | --- |")
    for card in cards:
        hour = _format_hour_label(card["hour_start"])
        apps = ", ".join(_parse_apps_json(card["apps_json"])) or "—"
        screens = str(card["screen_count"])
        voice_secs = int(card["audio_seconds"])
        voice = f"{voice_secs // 60}m" if voice_secs > 0 else "—"
        words = card["top_words"] or "—"
        # Pipes inside cell text would break the markdown table — replace
        # with a backslash-escaped pipe so renderers keep one row per card.
        apps_safe = apps.replace("|", "\\|")
        words_safe = words.replace("|", "\\|")
        out.append(
            f"| {hour} | {apps_safe} | {screens} | {voice} | {words_safe} |"
        )
    out.append("")
    return out


def _render_notes(notes: list[dict[str, Any]], entities: list[str]) -> list[str]:
    out: list[str] = [_h(2, "Notes")]
    if not notes:
        out.append("_No notes for this day._")
        out.append("")
        return out
    for note in notes:
        title = note["title"] or f"Note #{note['id']}"
        out.append(_h(3, title))
        meta: list[str] = [_format_clock(note["created_at"])]
        if note["source"]:
            meta.append(f"source: {note['source']}")
        if note["encrypted"]:
            meta.append("[locked]")
        out.append("_" + " · ".join(meta) + "_")
        out.append("")
        if note["encrypted"]:
            out.append("_(encrypted — body not exported)_")
        else:
            out.append(_wikilink_entities(note["body"], entities))
            if note["truncated"]:
                out.append("")
                out.append(
                    f"_(truncated to {_MAX_NOTE_BODY_CHARS} chars — open "
                    "in Persona for the full note)_"
                )
        out.append("")
    return out


def _render_pinned_shots(pinned: list[dict[str, Any]]) -> list[str]:
    out: list[str] = [_h(2, "Pinned shots")]
    if not pinned:
        out.append("_No pinned shots for this day._")
        out.append("")
        return out
    for shot in pinned:
        clock = _format_clock(shot["captured_at"])
        context_parts: list[str] = []
        if shot["app_name"]:
            context_parts.append(shot["app_name"])
        if shot["window_title"]:
            context_parts.append(shot["window_title"])
        context = " — ".join(context_parts) if context_parts else "shot"
        link = f"[shot #{shot['id']}](/shot/{shot['id']})"
        out.append(f"- {clock} · {link} · {context}")
    out.append("")
    return out


def _render_reactions(reactions: list[dict[str, Any]]) -> list[str]:
    out: list[str] = [_h(2, "Reactions")]
    if not reactions:
        out.append("_No reactions for this day._")
        out.append("")
        return out
    for row in reactions:
        emoji = row["emoji"] or "?"
        count = row["count"]
        last_id = row["last_shot_id"]
        link = f" — latest [shot #{last_id}](/shot/{last_id})" if last_id else ""
        out.append(f"- {emoji} x {count}{link}")
    out.append("")
    return out


def _render_long_reads(long_reads: list[dict[str, Any]]) -> list[str]:
    out: list[str] = [_h(2, "Long reads")]
    if not long_reads:
        out.append("_No long reads for this day._")
        out.append("")
        return out
    for session in long_reads:
        started = _format_clock(session["started_at"])
        ended = _format_clock(session["ended_at"])
        duration = _format_duration(session["duration_seconds"])
        title = session["window_title"] or "(untitled)"
        app = session["app_name"] or ""
        suffix = f" · {app}" if app else ""
        first_id = session["first_shot_id"]
        link = (
            f" — jump to [shot #{first_id}](/shot/{first_id})"
            if first_id
            else ""
        )
        out.append(
            f"- {started}-{ended} ({duration}) · {title}{suffix}{link}"
        )
    out.append("")
    return out


def _render_top_keywords(top_words: list[str]) -> list[str]:
    out: list[str] = [_h(2, "Top OCR keywords")]
    if not top_words:
        out.append("_No keywords for this day._")
        out.append("")
        return out
    out.append(", ".join(top_words))
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Renderer + public API
# ---------------------------------------------------------------------------


def _has_any_section(
    *,
    digest: str | None,
    pin: dict[str, str | None] | None,
    hourly: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    pinned: list[dict[str, Any]],
    reactions: list[dict[str, Any]],
    long_reads: list[dict[str, Any]],
    top_words: list[str],
) -> bool:
    """Return ``True`` if at least one section has real content.

    The route layer uses this to decide between rendering a document
    and returning a 404 — an empty day with zero shots, zero notes,
    and zero hourly cards is not a journal entry, it's a missing day.
    """
    return any(
        (
            digest,
            pin,
            hourly,
            notes,
            pinned,
            reactions,
            long_reads,
            top_words,
        )
    )


async def _render(conn: aiosqlite.Connection, day: date) -> str | None:
    day_iso = day.isoformat()

    digest = await _fetch_daily_digest(conn, day_iso)
    pin = await _fetch_daily_pin(conn, day_iso)
    hourly = await _fetch_hourly_cards(conn, day)
    notes = await _fetch_notes(conn, day)
    pinned = await _fetch_pinned_shots(conn, day)
    reactions = await _fetch_reactions(conn, day)
    long_reads = await _fetch_long_reads(conn, day)
    entities = await _fetch_known_entities(conn)
    top_words = _top_keywords_across_hours(hourly)

    if not _has_any_section(
        digest=digest,
        pin=pin,
        hourly=hourly,
        notes=notes,
        pinned=pinned,
        reactions=reactions,
        long_reads=long_reads,
        top_words=top_words,
    ):
        return None

    lines: list[str] = []
    lines.extend(_render_header(day_iso))
    lines.extend(_render_tldr(digest, entities))
    lines.extend(_render_daily_pin(pin, entities))
    lines.extend(_render_hourly_table(hourly))
    lines.extend(_render_notes(notes, entities))
    lines.extend(_render_pinned_shots(pinned))
    lines.extend(_render_reactions(reactions))
    lines.extend(_render_long_reads(long_reads))
    lines.extend(_render_top_keywords(top_words))

    return "\n".join(lines).rstrip() + "\n"


async def build_day_md(day_iso: str) -> str:
    """Return the markdown body for ``day_iso`` as one string.

    ``day_iso`` must be a ``YYYY-MM-DD`` literal; anything else raises
    :class:`ValueError` and the route layer turns it into a 400. The
    return value is an empty string when the day has no exportable
    content — the caller is expected to treat empty as a 404 rather
    than serve a one-section placeholder file.
    """
    day = _parse_day(day_iso)
    async with get_connection() as conn:
        body = await _render(conn, day)
    if body is None:
        log.info("day_markdown.empty", day=day_iso)
        return ""
    log.info(
        "day_markdown.built",
        day=day_iso,
        bytes=len(body.encode("utf-8")),
    )
    return body


__all__ = ["build_day_md"]
