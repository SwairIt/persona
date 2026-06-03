"""Free-form text search across sticky-note bodies.

Endpoint:

* ``GET /stickers/search?q=TERM`` — HTML page rendering up to
  :data:`_MAX_RESULTS` matches, each row showing the thumbnail of the
  parent screenshot and an excerpt of the sticky body with the matched
  term wrapped in ``<mark>``.

Why a separate endpoint (not an extension of ``/stickers``)
----------------------------------------------------------
The cross-shot gallery at ``/stickers`` (see
:mod:`app.web.routes.stickers_gallery`) is a "show me everything,
newest first" browser. Search is a different intent — "find the one
note where I mentioned X" — and dropping a query box on the gallery
would either (a) reuse the same tile grid (visually noisy when only
two tiles match) or (b) reflow the gallery into a list view (jarring
context switch). Splitting at the URL level keeps both pages honest
about what they are for.

Implementation notes
--------------------
* The match is a parametrised ``LIKE %q%`` against ``sticky_note.body``,
  wrapped with ``LOWER(...)`` on both sides so the search is
  case-insensitive for non-ASCII text as well (SQLite's default
  ``LIKE`` is ASCII-only case-insensitive).
* ``%`` and ``_`` are LIKE wildcards; we escape them with a literal
  ``\\`` and pass ``ESCAPE '\\\\'`` so a query containing ``50%`` finds
  the literal substring rather than "anything containing 50".
* The ``<mark>`` highlighting is built in Python *after* HTML-escaping
  the body so the template can render the excerpt with ``| safe``
  without exposing it to injection — the only HTML in the excerpt is
  the literal ``<mark>`` / ``</mark>`` tags we add ourselves.
* ``LIMIT 100`` is hard-coded; the input ``q`` is never spliced into
  SQL.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.sticky_search")

router = APIRouter(tags=["sticky_search"])

# Cap on rendered rows. Sticky-note bodies are short (max 2000 chars on
# insert) and most users will have at most a few hundred notes total —
# a 100-row cap is comfortably larger than any realistic match set
# while keeping the HTML payload bounded.
_MAX_RESULTS = 100

# Excerpt window around the first match, in characters. Generous enough
# to give context, tight enough that ten matches still fit on a screen
# without scrolling each tile.
_EXCERPT_RADIUS = 80

# Hard cap on the raw query string to keep abusive inputs out of the
# log line and out of the LIKE pattern. Anything beyond this is a
# misclick or an attack, not a real search.
_MAX_QUERY_LEN = 200


_SELECT_MATCHES = (
    "SELECT "
    "  sn.id            AS sticky_id, "
    "  sn.shot_id       AS shot_id, "
    "  sn.body          AS body, "
    "  sn.color         AS color, "
    "  sn.created_at    AS created_at, "
    "  s.thumbnail_path AS thumbnail_path, "
    "  s.app_name       AS app_name, "
    "  s.window_title   AS window_title, "
    "  s.captured_at    AS captured_at "
    "FROM sticky_note AS sn "
    "LEFT JOIN screenshots AS s ON s.id = sn.shot_id "
    "WHERE LOWER(sn.body) LIKE LOWER(?) ESCAPE '\\' "
    "ORDER BY sn.id DESC "
    "LIMIT ?"
)


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so the search is a literal substring.

    SQLite's ``LIKE`` treats ``%`` and ``_`` (and the escape char itself)
    as metacharacters. Without escaping, a query like ``50%`` would
    match any body starting with ``50`` — confusing UX. We escape with
    a backslash and the SQL declares ``ESCAPE '\\'`` to honour it.
    """
    return (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _build_excerpt(body: str, term: str, radius: int) -> str:
    """Return an HTML-safe excerpt with every match of ``term`` marked.

    Strategy:
      1. Locate the first case-insensitive match to centre the window.
         If there is no match (shouldn't happen — the SQL filter caught
         it — but defence in depth), return an escaped prefix of the
         body so the row still renders gracefully.
      2. Slice a window of ``radius`` characters either side of the
         first match.
      3. HTML-escape the slice, then wrap every case-insensitive
         occurrence of the term inside the slice with ``<mark>``.

    The escape-then-mark order is deliberate: escaping first means any
    ``<`` / ``>`` / ``&`` in the user's note becomes inert text, and
    the only HTML in the result is the literal ``<mark>`` tags this
    function emits. The template can render it with ``| safe``.
    """
    if not term:
        # No term to highlight — fall back to a plain truncated excerpt.
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

    # HTML-escape the slice, then highlight every case-insensitive
    # occurrence inside it. We use re.sub with a callable so the
    # original casing of each hit is preserved in the rendered output.
    escaped_slice = html.escape(slice_)
    # The term may contain regex metacharacters (``.``, ``*``, etc.) —
    # users are not writing regexes here, so ``re.escape`` makes the
    # pattern literal. The term is also HTML-escaped for the regex
    # search so the pattern aligns with the escaped slice.
    escaped_term = html.escape(term)
    pattern = re.compile(re.escape(escaped_term), re.IGNORECASE)
    marked = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped_slice)

    return prefix_ellipsis + marked + suffix_ellipsis


def _row_to_hit(row: aiosqlite.Row, term: str) -> dict[str, Any]:
    body_raw = str(row["body"])
    return {
        "sticky_id": int(row["sticky_id"]),
        "shot_id": int(row["shot_id"]),
        "body_full": body_raw,
        "excerpt": _build_excerpt(body_raw, term, _EXCERPT_RADIUS),
        "color": str(row["color"]),
        "created_at": str(row["created_at"]),
        "thumbnail_path": row["thumbnail_path"],
        "app_name": row["app_name"],
        "window_title": row["window_title"],
        "captured_at": row["captured_at"],
    }


async def _run_search(term: str) -> list[dict[str, Any]]:
    """Execute the LIKE search and materialise hit rows."""
    pattern = f"%{_escape_like(term)}%"
    async with get_connection() as conn:
        cursor = await conn.execute(_SELECT_MATCHES, (pattern, _MAX_RESULTS))
        rows = await cursor.fetchall()
    return [_row_to_hit(row, term) for row in rows]


@router.get("/stickers/search", response_class=HTMLResponse)
async def sticky_search_page(
    request: Request,
    q: str = Query(default=""),
) -> HTMLResponse:
    """Render the sticky-note search page. Always 200, even on no input."""
    query = q.strip()[:_MAX_QUERY_LEN]
    results: list[dict[str, Any]] = []
    if query:
        results = await _run_search(query)
        log.info("sticky_search.html", query=query, results=len(results))

    return templates.TemplateResponse(
        request,
        "sticky_search.html",
        {
            "title": f"Sticker search: {query}" if query else "Search stickers",
            "active_nav": "search",
            "query": query,
            "results": results,
            "total": len(results),
            "max_results": _MAX_RESULTS,
        },
    )
