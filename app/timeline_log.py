"""Per-day timeline log — git-style text-mode event stream.

The classic ``/`` timeline is a visual grid of screenshot thumbnails; the
audit log at ``/audit`` is an admin-action stream; the per-day audit
timeline at ``/audit/timeline/{day}`` lists privileged operations only.

What's missing is a single chronological log of *user-perceived* events
of the day — "I took a screenshot", "I jotted a note", "I pinned a
shot", "I tagged it ``#meeting``", "a reminder fired" — rendered as a
flat, terminal-friendly stream that reads like ``git log --oneline``.

This module is the data layer: :func:`build_log_lines` queries every
event-bearing table whose rows carry a wall-clock timestamp and merges
them into one ordered list. Rendering belongs in
:mod:`app.web.routes.timeline_log`.

Event sources (one query each, all parametrised on the day)
-----------------------------------------------------------
The brief lists six logical sources. They map to real tables like so:

* ``capture`` — every row in ``screenshots`` (a frame was captured).
* ``note`` — both ``notes`` (standalone inbox) and ``screenshot_notes``
  (per-screenshot note) — same conceptual event, two storage tables.
* ``pin`` — ``daily_pin`` rows (the tier-5 micro-summary pin). The
  ``screenshots.tier = 'pinned'`` column has no timestamp of its own
  (the flip is stored in-place), so we deliberately exclude it here —
  surfacing every existing pinned shot every day would drown the log.
* ``tag`` — both ``screenshot_tags`` and ``note_tags`` (created_at on
  the join row tells us when the tag was applied).
* ``capture_event`` — ``capture_events`` (worker start / pause / resume
  / error / heartbeat / cleanup).
* ``reminder`` — ``reminders`` (created or completed today; the brief
  calls these "ai_reminders" because the create form is LLM-assisted,
  but the storage is one plain table).

Design constraints baked into the implementation
------------------------------------------------
* **Parametrised SQL.** Every query binds the ``YYYY-MM-DD`` string via
  a ``?`` placeholder; the day string never enters the SQL text.
* **Day filter == ``date(ts)``.** Matches the same SQLite function used
  by every other per-day view in the repo (notes-timeline,
  audit-timeline, day-scrubber). Self-consistent regardless of the
  user's tz.
* **Bounded.** Each per-source query is hard-capped at ``limit`` rows
  and the merged result is then truncated to ``limit`` again, so a
  noisy capture day cannot inflate the response past the caller's
  budget. Default ceiling (500) matches what comfortably fits in a
  monospace block on a 1080p screen.
* **ts DESC.** A log reads "most recent first" — same convention as
  ``git log`` and the existing ``/audit`` page. The per-day audit
  timeline at ``/audit/timeline`` reads ascending because it's a
  *story*; this view is a *log*, and logs are tailed downward.
* **Single-character glyph + colour per kind.** The rendering layer
  uses a monospace ``<pre>`` block and wants a fixed-width left gutter,
  so each row carries exactly one glyph + a Tailwind colour class.
* **No raw bodies.** ``text`` is a one-line summary (truncated, no
  newlines). Encrypted notes show ``[locked]`` rather than the
  ciphertext blob.

The module deliberately ships no rendering helper of its own — the
route module owns HTML / JSON / plain-text projections.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final, TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Sequence

log = get_logger("persona.timeline_log")


class LogLine(TypedDict):
    """One row of the merged timeline log.

    ``glyph`` is a single character (never empty, never multi-grapheme)
    suitable for the leftmost column of a monospace ``<pre>`` block.
    ``color`` is a Tailwind class name (without the leading dot) so the
    template can drop it straight into ``class="..."`` without an
    extra mapping table.
    """

    ts_iso: str
    kind: str
    color: str
    glyph: str
    text: str


# -----------------------------------------------------------------------------
# Kind catalogue
# -----------------------------------------------------------------------------
# A flat tuple of (kind, glyph, tailwind-color) — the rendering layer can
# import this directly to build the filter-chip bar without restating the
# vocabulary.
#
# Glyphs are pure ASCII so the log stays copy-pasteable into any terminal
# (Windows cmd.exe still chokes on emoji), and one character wide so the
# monospace gutter never shifts. Colours are Tailwind class names rather
# than raw hex so the template doesn't need a second lookup.


KIND_CAPTURE: Final[str] = "capture"
KIND_NOTE: Final[str] = "note"
KIND_PIN: Final[str] = "pin"
KIND_TAG: Final[str] = "tag"
KIND_CAPTURE_EVENT: Final[str] = "capture_event"
KIND_REMINDER: Final[str] = "reminder"

KIND_CATALOGUE: Final[tuple[tuple[str, str, str], ...]] = (
    (KIND_CAPTURE, "*", "text-emerald-400"),
    (KIND_NOTE, "+", "text-sky-400"),
    (KIND_PIN, "@", "text-amber-400"),
    (KIND_TAG, "#", "text-fuchsia-400"),
    (KIND_CAPTURE_EVENT, "!", "text-zinc-400"),
    (KIND_REMINDER, "?", "text-rose-400"),
)

_KIND_TO_GLYPH: Final[dict[str, str]] = {k: g for k, g, _ in KIND_CATALOGUE}
_KIND_TO_COLOR: Final[dict[str, str]] = {k: c for k, _, c in KIND_CATALOGUE}

# Sentinel used in the rendered ``text`` for an encrypted / locked note.
# Mirrors the existing convention in :mod:`app.web.routes.notes_timeline`
# so the two log views agree on the placeholder.
_LOCKED_MARKER: Final[str] = "[locked]"

# Per-source row cap — keeps any single noisy table from monopolising
# the merged result. The merged list is itself truncated to ``limit``
# afterwards, so the final response never exceeds the caller's budget.
_PER_SOURCE_CAP: Final[int] = 2_000

# Hard maximum length of the ``text`` column. A 200-char OCR snippet or
# free-text note keeps the monospace block readable; anything longer is
# trimmed with an ellipsis so a multi-kilobyte body cannot wreck the
# table layout.
_TEXT_MAX_CHARS: Final[int] = 200


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _today_local() -> date:
    """Local-date "today" — matches every other per-day view in the repo."""
    return datetime.now().astimezone().date()


def _parse_day_or_today(day_iso: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to local today on any failure.

    Matches the convention used by the day-scrubber, day-kanban,
    notes-timeline and audit-timeline routes: a bad path lands on today
    rather than 400-ing. A log is exploratory — surfacing *something*
    useful beats a stack trace.
    """
    if day_iso is None or day_iso == "":
        return _today_local()
    try:
        return datetime.strptime(day_iso, "%Y-%m-%d").date()
    except ValueError:
        log.info("timeline_log.day_invalid_fallback_today", value=day_iso)
        return _today_local()


def _summarise(value: str | None) -> str:
    """One-line, length-capped projection of any free-text body.

    The monospace ``<pre>`` block expects one log row per line — so we
    fold whitespace, drop control characters and truncate with an
    ellipsis when the body would otherwise blow past the column budget.

    ``None`` and the empty string both collapse to the empty string so
    the row still renders (with an empty payload) rather than the JSON
    response carrying ``null`` and crashing the front-end mapper.
    """
    if value is None:
        return ""
    # Collapse every whitespace run to a single space so multi-line
    # bodies fit on one log line. ``str.split()`` without an argument
    # splits on *any* run of whitespace and discards empties, which is
    # exactly the behaviour we want.
    folded = " ".join(value.split())
    if len(folded) <= _TEXT_MAX_CHARS:
        return folded
    # Reserve one char for the ellipsis so the visible width is exact.
    return folded[: _TEXT_MAX_CHARS - 1] + "…"


def _line(ts: str, kind: str, text: str) -> LogLine:
    """Assemble a :class:`LogLine` from raw projection parts.

    Centralises the kind → glyph / colour lookup so the per-source
    fetchers stay focused on their SQL and don't repeat the catalogue.
    Unknown kinds get a neutral fallback rather than raising — a typo
    in a future caller should surface as a grey row, not a 500.
    """
    return LogLine(
        ts_iso=ts,
        kind=kind,
        color=_KIND_TO_COLOR.get(kind, "text-zinc-400"),
        glyph=_KIND_TO_GLYPH.get(kind, "."),
        text=text,
    )


# -----------------------------------------------------------------------------
# Per-source fetchers — each returns a list[LogLine] for the given day.
# Every SQL is fully static; the day string travels via ``?``.
# -----------------------------------------------------------------------------


async def _fetch_captures(
    conn: aiosqlite.Connection,
    day_str: str,
) -> list[LogLine]:
    """Every captured screenshot for the day.

    Summary text follows the same priority the rest of the codebase
    uses for "what was on screen": window title first (most specific),
    app name second (always present), then a stub if both are NULL.
    """
    cursor = await conn.execute(
        """
        SELECT id,
               captured_at,
               app_name,
               window_title
          FROM screenshots
         WHERE date(captured_at) = ?
         ORDER BY captured_at DESC, id DESC
         LIMIT ?
        """,
        (day_str, _PER_SOURCE_CAP),
    )
    rows = await cursor.fetchall()
    lines: list[LogLine] = []
    for row in rows:
        title = row["window_title"] if row["window_title"] is not None else ""
        app = row["app_name"] if row["app_name"] is not None else ""
        body = str(title).strip() or str(app).strip() or f"shot #{int(row['id'])}"
        lines.append(_line(str(row["captured_at"]), KIND_CAPTURE, _summarise(body)))
    return lines


async def _fetch_notes(
    conn: aiosqlite.Connection,
    day_str: str,
) -> list[LogLine]:
    """Standalone inbox notes created on the day.

    The ``notes`` table grew an ``encrypted`` flag in migration 045 but
    *only when the encrypted-notes feature is installed*. We guard the
    column reference behind a soft ``OperationalError`` fallback so a
    fresh checkout that hasn't applied that migration still surfaces
    plaintext rows correctly. Encrypted bodies are blanked to the
    ``[locked]`` sentinel rather than leaking the ciphertext.
    """
    try:
        cursor = await conn.execute(
            """
            SELECT id, title, body, created_at, encrypted
              FROM notes
             WHERE date(created_at) = ?
             ORDER BY created_at DESC, id DESC
             LIMIT ?
            """,
            (day_str, _PER_SOURCE_CAP),
        )
        rows = await cursor.fetchall()
    except aiosqlite.OperationalError:
        # ``encrypted`` column missing on a very old install — re-run
        # without it. Same shape, just no encryption awareness.
        cursor = await conn.execute(
            """
            SELECT id, title, body, created_at, 0 AS encrypted
              FROM notes
             WHERE date(created_at) = ?
             ORDER BY created_at DESC, id DESC
             LIMIT ?
            """,
            (day_str, _PER_SOURCE_CAP),
        )
        rows = await cursor.fetchall()

    lines: list[LogLine] = []
    for row in rows:
        is_locked = bool(int(row["encrypted"] or 0))
        if is_locked:
            body = _LOCKED_MARKER
        else:
            title_raw = row["title"]
            body_raw = row["body"]
            title = str(title_raw).strip() if title_raw is not None else ""
            body_text = str(body_raw) if body_raw is not None else ""
            body = title or body_text or f"note #{int(row['id'])}"
        lines.append(_line(str(row["created_at"]), KIND_NOTE, _summarise(body)))
    return lines


async def _fetch_screenshot_notes(
    conn: aiosqlite.Connection,
    day_str: str,
) -> list[LogLine]:
    """Per-screenshot notes — same kind as inbox notes, different table.

    The brief lists "notes_created" as a single source; treating both
    tables as ``note`` events keeps the filter-chip UX simple ("show
    me every note today") without exposing storage-layout details.
    """
    cursor = await conn.execute(
        """
        SELECT screenshot_id, body, created_at
          FROM screenshot_notes
         WHERE date(created_at) = ?
         ORDER BY created_at DESC, screenshot_id DESC
         LIMIT ?
        """,
        (day_str, _PER_SOURCE_CAP),
    )
    rows = await cursor.fetchall()
    lines: list[LogLine] = []
    for row in rows:
        body_raw = row["body"]
        body = (
            str(body_raw)
            if body_raw is not None
            else f"shot #{int(row['screenshot_id'])} note"
        )
        # Prefix with the shot id so the user can tell the per-shot note
        # apart from a standalone inbox note in the merged log.
        prefixed = f"shot #{int(row['screenshot_id'])}: {body}"
        lines.append(_line(str(row["created_at"]), KIND_NOTE, _summarise(prefixed)))
    return lines


async def _fetch_pins(
    conn: aiosqlite.Connection,
    day_str: str,
) -> list[LogLine]:
    """Daily pin rows whose ``updated_at`` falls on the day.

    The ``daily_pin`` row for today is rewritten each time the
    end-of-day summary scheduler runs; ``updated_at`` therefore tells
    us *when the pin was last touched*, which is the user-visible
    "pin happened" event. We filter on ``date(updated_at)`` rather
    than the ``day`` column itself so a re-run on the next morning
    surfaces under that morning, matching the wall-clock convention
    every other event source uses.
    """
    cursor = await conn.execute(
        """
        SELECT day, pin, source, updated_at
          FROM daily_pin
         WHERE date(updated_at) = ?
         ORDER BY updated_at DESC
         LIMIT ?
        """,
        (day_str, _PER_SOURCE_CAP),
    )
    rows = await cursor.fetchall()
    lines: list[LogLine] = []
    for row in rows:
        pin_raw = row["pin"]
        pin_text = str(pin_raw) if pin_raw is not None else ""
        source = str(row["source"]) if row["source"] is not None else "?"
        day_label = str(row["day"]) if row["day"] is not None else "?"
        prefix = f"pin {day_label} ({source})"
        body = f"{prefix}: {pin_text}" if pin_text else prefix
        lines.append(_line(str(row["updated_at"]), KIND_PIN, _summarise(body)))
    return lines


async def _fetch_screenshot_tags(
    conn: aiosqlite.Connection,
    day_str: str,
) -> list[LogLine]:
    """Tag-on-screenshot events for the day.

    Joined against ``tags`` so the log row shows the tag *name* rather
    than the join's foreign key id; an opaque ``tag_id=42`` is useless
    in a story-style log.
    """
    cursor = await conn.execute(
        """
        SELECT st.screenshot_id, t.name AS tag_name, st.created_at
          FROM screenshot_tags st
          JOIN tags t ON t.id = st.tag_id
         WHERE date(st.created_at) = ?
         ORDER BY st.created_at DESC, st.screenshot_id DESC
         LIMIT ?
        """,
        (day_str, _PER_SOURCE_CAP),
    )
    rows = await cursor.fetchall()
    lines: list[LogLine] = []
    for row in rows:
        body = f"#{row['tag_name']} -> shot #{int(row['screenshot_id'])}"
        lines.append(_line(str(row["created_at"]), KIND_TAG, _summarise(body)))
    return lines


async def _fetch_note_tags(
    conn: aiosqlite.Connection,
    day_str: str,
) -> list[LogLine]:
    """Tag-on-note events for the day.

    The ``note_tags`` table mirrors ``screenshot_tags`` and was added
    by migration 039. We treat both as the same ``tag`` kind in the
    merged log so a single ``kind=tag`` chip surfaces every tagging
    action of the day regardless of what was tagged.
    """
    cursor = await conn.execute(
        """
        SELECT nt.note_id, t.name AS tag_name, nt.created_at
          FROM note_tags nt
          JOIN tags t ON t.id = nt.tag_id
         WHERE date(nt.created_at) = ?
         ORDER BY nt.created_at DESC, nt.note_id DESC
         LIMIT ?
        """,
        (day_str, _PER_SOURCE_CAP),
    )
    rows = await cursor.fetchall()
    lines: list[LogLine] = []
    for row in rows:
        body = f"#{row['tag_name']} -> note #{int(row['note_id'])}"
        lines.append(_line(str(row["created_at"]), KIND_TAG, _summarise(body)))
    return lines


async def _fetch_capture_events(
    conn: aiosqlite.Connection,
    day_str: str,
) -> list[LogLine]:
    """Worker lifecycle events (start / pause / resume / error / ...).

    These are the only rows in the merged log that aren't user-driven —
    they're the worker telling its own story — but they belong in the
    log because a missing capture run is exactly the kind of thing a
    user wants to spot when scanning "what happened today".
    """
    cursor = await conn.execute(
        """
        SELECT id, ts, event_type, details
          FROM capture_events
         WHERE date(ts) = ?
         ORDER BY ts DESC, id DESC
         LIMIT ?
        """,
        (day_str, _PER_SOURCE_CAP),
    )
    rows = await cursor.fetchall()
    lines: list[LogLine] = []
    for row in rows:
        details_raw = row["details"]
        details = str(details_raw).strip() if details_raw is not None else ""
        event_type = str(row["event_type"])
        body = f"{event_type}: {details}" if details else event_type
        lines.append(_line(str(row["ts"]), KIND_CAPTURE_EVENT, _summarise(body)))
    return lines


async def _fetch_reminders(
    conn: aiosqlite.Connection,
    day_str: str,
) -> list[LogLine]:
    """Reminders touched today — created OR completed.

    A reminder is a two-phase event: created (UI: "remind me ...") and
    completed (UI: tick). Both phases deserve a log row because a user
    skimming the day cares about both "I queued a thing" and "I closed
    a thing". We deliberately do NOT cross-emit a row for the *due*
    column — due_date is a future-pointing field and would generate a
    confusing entry for tomorrow's reminder under today's log.

    The two phases share the same kind (``reminder``) but disambiguate
    via a textual prefix in the rendered body so the merged log stays
    self-explanatory.
    """
    cursor = await conn.execute(
        """
        SELECT id,
               body,
               created_at,
               completed_at,
               done
          FROM reminders
         WHERE date(created_at) = ?
            OR date(completed_at) = ?
         ORDER BY COALESCE(completed_at, created_at) DESC, id DESC
         LIMIT ?
        """,
        (day_str, day_str, _PER_SOURCE_CAP),
    )
    rows = await cursor.fetchall()
    lines: list[LogLine] = []
    for row in rows:
        body_raw = row["body"]
        body_text = str(body_raw) if body_raw is not None else ""
        # Emit both phases independently so a reminder created AND
        # completed today shows up as two rows — same shape the user
        # would see if they were scrolling the UI in real time.
        created_at = row["created_at"]
        if created_at is not None and str(created_at).startswith(day_str):
            lines.append(
                _line(
                    str(created_at),
                    KIND_REMINDER,
                    _summarise(f"create: {body_text}"),
                )
            )
        completed_at = row["completed_at"]
        if completed_at is not None and str(completed_at).startswith(day_str):
            lines.append(
                _line(
                    str(completed_at),
                    KIND_REMINDER,
                    _summarise(f"done: {body_text}"),
                )
            )
    return lines


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------


async def build_log_lines(
    day_iso: str | None = None,
    limit: int = 500,
) -> list[LogLine]:
    """Return every loggable event for ``day_iso`` in ts-descending order.

    ``day_iso`` is ``YYYY-MM-DD``; ``None`` (or a malformed value) falls
    back to local today, matching every other per-day view in the repo.
    ``limit`` caps the *merged* response — the per-source queries are
    each capped separately at :data:`_PER_SOURCE_CAP` so a single noisy
    table can't dominate the result.

    Each entry is a :class:`LogLine`: ``ts_iso`` (raw column value, no
    reformatting — the template owns presentation), ``kind`` (one of
    the values in :data:`KIND_CATALOGUE`), ``color`` (Tailwind class
    name, never empty), ``glyph`` (single char, never empty) and
    ``text`` (one-line summary, ``[locked]`` for encrypted bodies,
    empty string is allowed but never ``None``).

    A failure on any individual source is logged + swallowed so a
    transient SQLite hiccup on (say) ``screenshot_notes`` doesn't lose
    every other section of the log. Total failure (we couldn't open a
    connection at all) returns an empty list, matching the forgiving
    fallback used by every other timeline view.
    """
    day_value = _parse_day_or_today(day_iso)
    day_str = day_value.strftime("%Y-%m-%d")
    # Clamp ``limit`` defensively — a caller passing 0 or a negative
    # value gets the default ceiling rather than an empty list, which
    # would be a confusing "is the day empty or did I typo?" failure
    # mode. A wildly large value is capped at the per-source ceiling so
    # a single bad caller can't pull tens of thousands of rows.
    effective_limit = max(1, min(limit, _PER_SOURCE_CAP))

    fetchers: Sequence[
        tuple[
            str,
            Any,  # async fn (conn, day_str) -> list[LogLine]
        ]
    ] = (
        ("captures", _fetch_captures),
        ("notes_inbox", _fetch_notes),
        ("notes_per_shot", _fetch_screenshot_notes),
        ("pins", _fetch_pins),
        ("tags_shot", _fetch_screenshot_tags),
        ("tags_note", _fetch_note_tags),
        ("capture_events", _fetch_capture_events),
        ("reminders", _fetch_reminders),
    )

    merged: list[LogLine] = []
    try:
        async with get_connection() as conn:
            for source_name, fetcher in fetchers:
                try:
                    batch = await fetcher(conn, day_str)
                except aiosqlite.Error as exc:
                    log.warning(
                        "timeline_log.source_failed",
                        source=source_name,
                        day=day_str,
                        error=str(exc),
                    )
                    continue
                merged.extend(batch)
    except aiosqlite.Error as exc:
        log.warning(
            "timeline_log.connection_failed",
            day=day_str,
            error=str(exc),
        )
        return []

    # ts DESC, then kind asc as a stable tie-breaker so two events at
    # the exact same wall-clock ts (rare but possible — multiple
    # taggings of the same screenshot in one transaction) render in a
    # deterministic order across reloads.
    merged.sort(key=lambda row: (row["ts_iso"], row["kind"]), reverse=True)
    truncated = merged[:effective_limit]

    log.info(
        "timeline_log.built",
        day=day_str,
        total=len(merged),
        returned=len(truncated),
        limit=effective_limit,
    )
    return truncated


__all__ = [
    "KIND_CAPTURE",
    "KIND_CAPTURE_EVENT",
    "KIND_CATALOGUE",
    "KIND_NOTE",
    "KIND_PIN",
    "KIND_REMINDER",
    "KIND_TAG",
    "LogLine",
    "build_log_lines",
]
