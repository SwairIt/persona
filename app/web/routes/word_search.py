"""Per-word search across the ``ocr_word`` table — v1.4.

Endpoint
--------
* ``GET /search/word?w=TERM&min_conf=80`` — render a Tailwind page that
  lists every screenshot whose ``ocr_word`` rows contain ``word LIKE
  TERM`` with ``conf >= min_conf``.

Why a dedicated route?
~~~~~~~~~~~~~~~~~~~~~~
The existing OCR full-text search ranks whole documents via the FTS5
``screenshots_fts`` index — great for "deploy plan", useless for "show
me every shot where Tesseract was *certain* it saw the word
``Kubernetes`` (conf >= 80)".  Per-word OCR rows (``ocr_word``,
v0.35) carry the actual confidence score; this route surfaces that
data so the operator can hunt for high-confidence sightings of a
single token, e.g. to triage a leaked password across a day's
captures.

Safety
------
* The user-supplied ``w`` is bound as a parameter and wrapped in
  ``%`` for ``LIKE``-based substring matching.  We never splice it
  into SQL.
* SQLite ``LIKE`` wildcards (``%`` and ``_``) inside ``w`` are
  escaped so a user pasting an underscore-laden filename does not
  accidentally turn it into a wildcard.  The ``ESCAPE '\\'`` clause
  spells that out for the planner.
* ``min_conf`` is clamped to ``0..100`` — Tesseract's score range —
  so a hostile querystring cannot push the comparison out of bounds.
* Results are capped at 100 rows so a degenerate single-letter query
  (``w=a``) does not blow up the page.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.word_search")

router = APIRouter(tags=["word-search"])

# Hard upper bound on rendered rows.  Mirrors the per-source cap on
# ``/search/everything`` so the layout stays predictable.
_MAX_RESULTS = 100

# Maximum length of the inbound term.  Tesseract tokens are rarely
# longer than ~40 characters; 64 is a comfortable ceiling that still
# rules out abuse via multi-kilobyte querystrings.
_MAX_TERM_LEN = 64

# Default ``min_conf`` if the operator omits it.  Matches the green
# threshold used by the per-shot overlay (see ``035_ocr_words.sql``).
_DEFAULT_MIN_CONF = 80


def _escape_like(term: str) -> str:
    """Escape SQLite ``LIKE`` metacharacters in ``term``.

    SQLite treats ``%`` and ``_`` as wildcards inside ``LIKE`` patterns
    and ``\\`` as the (configurable) escape character.  Without this
    sanitiser a search for ``foo_bar`` would also match ``fooXbar``,
    which is surprising.  The matching SQL uses ``ESCAPE '\\'``.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp_conf(value: int) -> int:
    """Pin ``value`` to the Tesseract confidence range ``0..100``."""
    if value < 0:
        return 0
    if value > 100:
        return 100
    return value


async def _run_word_search(
    conn: Any,
    term: str,
    min_conf: int,
) -> list[dict[str, Any]]:
    """Return up to :data:`_MAX_RESULTS` shots whose ``ocr_word`` row matches.

    Each result is the *best* (highest-confidence) matching word per
    screenshot, so the operator sees one row per shot instead of one
    row per token.  Sorted by that best confidence descending, with
    the most recent capture breaking ties.
    """
    pattern = f"%{_escape_like(term)}%"
    sql = (
        "SELECT s.id AS id, "
        "       s.captured_at AS captured_at, "
        "       s.thumbnail_path AS thumbnail_path, "
        "       s.app_name AS app_name, "
        "       s.window_title AS window_title, "
        "       MAX(w.conf) AS best_conf, "
        "       (SELECT w2.word FROM ocr_word w2 "
        "         WHERE w2.screenshot_id = s.id "
        "           AND w2.word LIKE ? ESCAPE '\\' "
        "           AND w2.conf >= ? "
        "         ORDER BY w2.conf DESC, w2.id ASC LIMIT 1) AS matched_word "
        "FROM ocr_word w "
        "JOIN screenshots s ON s.id = w.screenshot_id "
        "WHERE w.word LIKE ? ESCAPE '\\' "
        "  AND w.conf >= ? "
        "GROUP BY s.id "
        "ORDER BY best_conf DESC, s.captured_at DESC "
        "LIMIT ?"
    )
    params: tuple[Any, ...] = (
        pattern,
        min_conf,
        pattern,
        min_conf,
        _MAX_RESULTS,
    )
    try:
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
    except Exception as exc:
        # Any DB error (missing migration, locked DB) becomes an empty
        # render rather than a 500 — the page exists primarily for
        # exploration and a hard failure here would surprise the user.
        log.warning(
            "word_search.query_failed",
            term=term,
            min_conf=min_conf,
            error=str(exc),
        )
        return []

    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]) if row["captured_at"] is not None else "",
            "thumbnail_path": (
                str(row["thumbnail_path"]) if row["thumbnail_path"] is not None else None
            ),
            "app_name": str(row["app_name"]) if row["app_name"] is not None else "",
            "window_title": str(row["window_title"]) if row["window_title"] is not None else "",
            "best_conf": int(row["best_conf"]) if row["best_conf"] is not None else 0,
            "matched_word": (
                str(row["matched_word"]) if row["matched_word"] is not None else ""
            ),
        }
        for row in rows
    ]


@router.get("/search/word", response_class=HTMLResponse)
async def word_search_page(
    request: Request,
    w: str = Query(default=""),
    min_conf: int = Query(default=_DEFAULT_MIN_CONF),
) -> HTMLResponse:
    """Render the per-word search page.  Always 200, even on empty input."""
    term = (w or "").strip()[:_MAX_TERM_LEN]
    conf = _clamp_conf(int(min_conf))

    results: list[dict[str, Any]] = []
    if term:
        async with get_connection() as conn:
            results = await _run_word_search(conn, term, conf)
        log.info(
            "word_search.page",
            term=term,
            min_conf=conf,
            results=len(results),
        )

    return templates.TemplateResponse(
        request,
        "word_search.html",
        {
            "title": f"Word: {term}" if term else "Search by OCR word",
            "active_nav": "search",
            "term": term,
            "min_conf": conf,
            "results": results,
            "total": len(results),
            "limit": _MAX_RESULTS,
        },
    )


__all__ = ["router"]
