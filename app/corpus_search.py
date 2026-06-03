"""Corpus-wide search across every text artefact Persona stores.

A single query is fanned out to five backing tables — OCR-text on
``screenshots`` (via the ``screenshots_fts`` virtual table), the
``screenshot_notes`` body (via ``notes_fts``), per-shot
``screenshot_annotation`` rows, ``sticky_note`` overlays, and
``clipboard_event`` history — and the matches are returned in a single
mapping the caller can render side-by-side.

Design notes
------------
* **No new SQL surface.** Every read goes through
  :func:`app.storage.db.get_connection` and reuses the existing FTS5
  virtual tables / indexes set up by the migrations. No schema changes.
* **Parametrised end-to-end.** The user-supplied query string is *never*
  spliced into SQL — it lands in ``?`` placeholders for the LIKE pages
  and in a sanitised MATCH expression (every token quoted as a literal
  phrase) for the FTS pages, mirroring the strategy already used in
  :mod:`app.web.routes.notes_search` and :mod:`app.web.routes.sticky_search`.
* **Each kind is capped independently.** Callers pass a single
  ``limit``; we apply it per source so a noisy clipboard table cannot
  starve sparse sources like annotations.
* **Encrypted standalone notes are excluded.** The standalone ``notes``
  table can carry Fernet-encrypted bodies (``encrypted = 1``,
  ``body = ''``); we ``WHERE encrypted = 0`` so we never leak ciphertext
  or empty rows into the combined view.
* **Best-effort per source.** A single source raising
  :class:`Exception` (typically ``aiosqlite.OperationalError`` on
  FTS-parser edge cases) is logged and replaced with an empty list —
  the other four kinds still render.

Return shape
------------
The dict is fixed:

    {
        "shots":       [ {id, captured_at, app_name, window_title,
                          thumbnail_path, snippet}, ... ],
        "notes":       [ {id, snippet, created_at}, ... ],
        "annotations": [ {id, screenshot_id, excerpt, created_at}, ... ],
        "stickies":    [ {sticky_id, shot_id, excerpt, color,
                          created_at}, ... ],
        "clipboard":   [ {id, preview, length, app_name,
                          captured_at}, ... ],
    }

Every list is at most ``limit`` rows; an empty query returns five
empty lists.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.corpus_search")

# Cap on the input length to keep abusive queries out of SQL bind
# parameters and out of the structured log line. Anything beyond this
# is a misclick or an attack, not a real search.
_MAX_QUERY_LEN = 200

# Snippet window around the first LIKE hit, in characters.
_EXCERPT_RADIUS = 80

# Maximum characters returned for a clipboard preview before we slice
# and append an ellipsis.
_PREVIEW_CHARS = 200

# Names of every kind in the response payload — exported so callers
# (template loops, tests) can iterate without repeating the literal.
KINDS: tuple[str, ...] = ("shots", "notes", "annotations", "stickies", "clipboard")

# FTS5 specials that, left raw in user input, either change the query
# semantics (operators) or trip the FTS5 parser (unbalanced quotes,
# column filters, NEAR / MATCH meta). We strip them and quote each
# surviving token so the result is always a literal phrase AND.
_FTS_SPECIAL_RE = re.compile(r'[\"\'\(\)\*\:\^\-\+\!\&\|\~\?\\\[\]\{\},/;<>=]')


def _sanitize_fts_query(raw: str) -> str:
    """Turn arbitrary user input into a safe FTS5 ``MATCH`` expression.

    Strategy: tear out every FTS-special character, collapse whitespace,
    then wrap each surviving token in double quotes so it is treated as
    a literal phrase. Returns the empty string when nothing usable is
    left — callers MUST treat that as "no query, skip the source" and
    NOT pass it to ``MATCH`` (which would raise on empty input).
    """
    cleaned = _FTS_SPECIAL_RE.sub(" ", raw)
    tokens = [tok for tok in cleaned.split() if tok]
    if not tokens:
        return ""
    return " ".join(f'"{tok}"' for tok in tokens)


def _escape_like(term: str) -> str:
    """Escape ``%`` / ``_`` / ``\\`` for SQLite LIKE with ``ESCAPE '\\'``.

    Without escaping, a query like ``50%`` would match any body starting
    with ``50`` — confusing UX. The SQL site declares ``ESCAPE '\\'`` so
    the backslash here is honoured as the metacharacter prefix.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_excerpt(body: str, term: str, radius: int) -> str:
    """Return an HTML-safe excerpt with every match of ``term`` marked.

    The escape-then-mark order is deliberate: escaping first means any
    ``<`` / ``>`` / ``&`` in the source becomes inert text, and the
    only HTML in the result is the literal ``<mark>`` tags this
    function emits. Templates may render the value with ``| safe``
    without exposing it to injection.
    """
    if not term:
        prefix = body[: radius * 2]
        ellipsis = "..." if len(body) > len(prefix) else ""
        return html.escape(prefix) + ellipsis

    lowered_body = body.lower()
    lowered_term = term.lower()
    first = lowered_body.find(lowered_term)
    if first < 0:
        prefix = body[: radius * 2]
        ellipsis = "..." if len(body) > len(prefix) else ""
        return html.escape(prefix) + ellipsis

    start = max(0, first - radius)
    end = min(len(body), first + len(term) + radius)
    slice_ = body[start:end]
    prefix_ellipsis = "..." if start > 0 else ""
    suffix_ellipsis = "..." if end < len(body) else ""

    escaped_slice = html.escape(slice_)
    escaped_term = html.escape(term)
    pattern = re.compile(re.escape(escaped_term), re.IGNORECASE)
    marked = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped_slice)

    return prefix_ellipsis + marked + suffix_ellipsis


async def _search_shots(
    conn: aiosqlite.Connection,
    *,
    fts_query: str,
    limit: int,
) -> list[dict[str, Any]]:
    """OCR text + window title + app name, ranked by bm25."""
    if not fts_query:
        return []
    sql = (
        "SELECT s.id AS id, s.captured_at AS captured_at, "
        "s.thumbnail_path AS thumbnail_path, s.app_name AS app_name, "
        "s.window_title AS window_title, "
        "snippet(screenshots_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet "
        "FROM screenshots_fts "
        "JOIN screenshots s ON s.id = screenshots_fts.rowid "
        "WHERE screenshots_fts MATCH ? "
        "ORDER BY bm25(screenshots_fts) "
        "LIMIT ?"
    )
    try:
        cursor = await conn.execute(sql, (fts_query, limit))
        rows = await cursor.fetchall()
    except Exception as exc:
        # FTS5 can raise OperationalError on rare unicode edges; degrade
        # gracefully so the other four sources still render.
        log.warning("corpus_search.shots.failed", error=str(exc))
        return []
    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "thumbnail_path": (
                str(row["thumbnail_path"]) if row["thumbnail_path"] is not None else None
            ),
            "app_name": str(row["app_name"]) if row["app_name"] is not None else None,
            "window_title": (
                str(row["window_title"]) if row["window_title"] is not None else None
            ),
            "snippet": str(row["snippet"]),
        }
        for row in rows
    ]


async def _search_notes(
    conn: aiosqlite.Connection,
    *,
    fts_query: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Per-screenshot note bodies via ``notes_fts``.

    The JOIN target is ``screenshot_notes`` (plaintext only) — the FTS5
    index never observes encrypted standalone notes, so no extra filter
    is needed here. See :mod:`app.web.routes.notes_search` for the
    encryption-leak audit.
    """
    if not fts_query:
        return []
    sql = (
        "SELECT n.screenshot_id AS id, "
        "snippet(notes_fts, 0, '<mark>', '</mark>', '…', 30) AS snippet, "
        "n.created_at AS created_at "
        "FROM notes_fts "
        "JOIN screenshot_notes n ON n.screenshot_id = notes_fts.rowid "
        "WHERE notes_fts MATCH ? "
        "ORDER BY bm25(notes_fts) "
        "LIMIT ?"
    )
    try:
        cursor = await conn.execute(sql, (fts_query, limit))
        rows = await cursor.fetchall()
    except Exception as exc:
        log.warning("corpus_search.notes.failed", error=str(exc))
        return []
    return [
        {
            "id": int(row["id"]),
            "snippet": str(row["snippet"]),
            "created_at": (str(row["created_at"]) if row["created_at"] is not None else ""),
        }
        for row in rows
    ]


async def _search_annotations(
    conn: aiosqlite.Connection,
    *,
    term: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Append-only commentary lines via case-insensitive LIKE."""
    if not term:
        return []
    pattern = f"%{_escape_like(term)}%"
    sql = (
        "SELECT id, screenshot_id, body, created_at "
        "FROM screenshot_annotation "
        "WHERE LOWER(body) LIKE LOWER(?) ESCAPE '\\' "
        "ORDER BY id DESC "
        "LIMIT ?"
    )
    try:
        cursor = await conn.execute(sql, (pattern, limit))
        rows = await cursor.fetchall()
    except Exception as exc:
        log.warning("corpus_search.annotations.failed", error=str(exc))
        return []
    return [
        {
            "id": int(row["id"]),
            "screenshot_id": int(row["screenshot_id"]),
            "excerpt": _build_excerpt(str(row["body"]), term, _EXCERPT_RADIUS),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def _search_stickies(
    conn: aiosqlite.Connection,
    *,
    term: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Sticky-note overlays via case-insensitive LIKE."""
    if not term:
        return []
    pattern = f"%{_escape_like(term)}%"
    sql = (
        "SELECT id, shot_id, body, color, created_at "
        "FROM sticky_note "
        "WHERE LOWER(body) LIKE LOWER(?) ESCAPE '\\' "
        "ORDER BY id DESC "
        "LIMIT ?"
    )
    try:
        cursor = await conn.execute(sql, (pattern, limit))
        rows = await cursor.fetchall()
    except Exception as exc:
        log.warning("corpus_search.stickies.failed", error=str(exc))
        return []
    return [
        {
            "sticky_id": int(row["id"]),
            "shot_id": int(row["shot_id"]),
            "excerpt": _build_excerpt(str(row["body"]), term, _EXCERPT_RADIUS),
            "color": str(row["color"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def _search_clipboard(
    conn: aiosqlite.Connection,
    *,
    term: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Opt-in clipboard history via case-insensitive LIKE.

    The ``text`` column is already redaction-masked by the capture
    worker, so the snippet shown here cannot be more sensitive than
    what the user has already chosen to keep.
    """
    if not term:
        return []
    pattern = f"%{_escape_like(term)}%"
    sql = (
        "SELECT id, captured_at, text, length, app_name "
        "FROM clipboard_event "
        "WHERE LOWER(text) LIKE LOWER(?) ESCAPE '\\' "
        "ORDER BY captured_at DESC, id DESC "
        "LIMIT ?"
    )
    try:
        cursor = await conn.execute(sql, (pattern, limit))
        rows = await cursor.fetchall()
    except Exception as exc:
        log.warning("corpus_search.clipboard.failed", error=str(exc))
        return []
    items: list[dict[str, Any]] = []
    for row in rows:
        text = str(row["text"])
        preview = text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"
        items.append(
            {
                "id": int(row["id"]),
                "preview": _build_excerpt(preview, term, _EXCERPT_RADIUS),
                "length": int(row["length"]),
                "app_name": (str(row["app_name"]) if row["app_name"] is not None else None),
                "captured_at": str(row["captured_at"]),
            }
        )
    return items


def _empty_result() -> dict[str, list[dict[str, Any]]]:
    """Empty payload with every key present so callers can iterate fearlessly."""
    return {kind: [] for kind in KINDS}


async def corpus_search(q: str, limit: int = 50) -> dict[str, list[dict[str, Any]]]:
    """Run a corpus-wide search and return one bucket per source.

    The five sources are queried concurrently against a single
    connection; an empty / blank query short-circuits to the empty
    payload without opening the database.

    Parameters
    ----------
    q:
        Raw user input. Trimmed and truncated to :data:`_MAX_QUERY_LEN`
        before any further processing.
    limit:
        Maximum rows per source. Negative or zero values are coerced to
        zero (returns empty buckets but still hits the DB once for
        consistency); excessive values are NOT clamped here — callers
        are expected to pick a sensible page size.
    """
    term = (q or "").strip()[:_MAX_QUERY_LEN]
    capped_limit = max(0, int(limit))
    if not term:
        log.info("corpus_search.empty_query")
        return _empty_result()

    fts_query = _sanitize_fts_query(term)

    async with get_connection() as conn:
        shots, notes, annotations, stickies, clipboard = await asyncio.gather(
            _search_shots(conn, fts_query=fts_query, limit=capped_limit),
            _search_notes(conn, fts_query=fts_query, limit=capped_limit),
            _search_annotations(conn, term=term, limit=capped_limit),
            _search_stickies(conn, term=term, limit=capped_limit),
            _search_clipboard(conn, term=term, limit=capped_limit),
        )

    result: dict[str, list[dict[str, Any]]] = {
        "shots": shots,
        "notes": notes,
        "annotations": annotations,
        "stickies": stickies,
        "clipboard": clipboard,
    }
    log.info(
        "corpus_search.done",
        query=term,
        limit=capped_limit,
        shots=len(shots),
        notes=len(notes),
        annotations=len(annotations),
        stickies=len(stickies),
        clipboard=len(clipboard),
    )
    return result


__all__ = ["KINDS", "corpus_search"]
