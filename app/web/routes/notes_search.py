"""FTS5 full-text search over screenshot notes.

Exposes:
    GET /notes/search?q=...        — HTML page (notes_search.html)
    GET /api/notes/search.json?q=… — JSON payload for clients / extensions

Both routes are read-only, return HTTP 200 even on empty / invalid input, and
cap results at 50 ranked by bm25(notes_fts).

Encryption-leak audit (v0.45)
-----------------------------
The opt-in note encryption added in v0.45 lives in the standalone
``notes`` table — the FTS5 virtual table (``notes_fts``, defined in
``023_notes_fts.sql``) and its triggers operate exclusively on the
``screenshot_notes`` table, which has **no** ``encrypted`` / ``ciphertext``
columns. Encrypted standalone notes therefore cannot leak through this
endpoint:

  * The standalone-notes encryption flow stores ciphertext in
    ``notes.ciphertext`` (BLOB) and only ever wrote ``body = ''`` to
    ``notes.body``; ``notes_fts`` does not index this column.
  * The screenshot-notes path was not extended in v0.45 and remains
    plaintext-only, so its FTS index is unaffected.

Even so, the SELECT below intentionally JOINs through
``screenshot_notes`` (not the ``notes`` table) so an accidental future
refactor that points ``notes_fts`` at the standalone table would still
need to add an explicit ``encrypted = 0`` filter here. Reviewers: keep
that filter in mind if the JOIN target ever changes.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["notes-search"])

log = get_logger("persona.notes.search")

_MAX_RESULTS = 50

# FTS5 syntax characters that, left raw in user input, would either change the
# query semantics (operators) or trip the FTS5 parser (unbalanced quotes,
# column filters, NEAR/MATCH meta). We strip them and quote what's left.
_FTS_SPECIAL_RE = re.compile(r'[\"\'\(\)\*\:\^\-\+\!\&\|\~\?\\\[\]\{\},/;<>=]')


def _sanitize_fts_query(raw: str) -> str:
    """Turn arbitrary user input into a safe FTS5 MATCH expression.

    Strategy: tear out every FTS-special character, collapse whitespace, then
    wrap each surviving token in double quotes so it is treated as a literal
    phrase. Returns an empty string if nothing usable is left — callers MUST
    treat that as 'no query, render empty state' (do NOT pass it to MATCH).
    """
    cleaned = _FTS_SPECIAL_RE.sub(" ", raw)
    tokens = [tok for tok in cleaned.split() if tok]
    if not tokens:
        return ""
    # Quote each token; join with spaces (implicit AND in FTS5).
    return " ".join(f'"{tok}"' for tok in tokens)


async def _run_search(conn: Any, q: str) -> list[dict[str, Any]]:
    """Execute the FTS5 query and return ranked snippet rows."""
    match_expr = _sanitize_fts_query(q)
    if not match_expr:
        return []

    sql = """
        SELECT
            n.screenshot_id AS id,
            snippet(notes_fts, 0, '<mark>', '</mark>', '…', 30) AS snippet,
            n.created_at AS created_at
        FROM notes_fts
        JOIN screenshot_notes n ON n.screenshot_id = notes_fts.rowid
        WHERE notes_fts MATCH ?
        ORDER BY bm25(notes_fts)
        LIMIT ?
    """
    try:
        cursor = await conn.execute(sql, (match_expr, _MAX_RESULTS))
        rows = await cursor.fetchall()
    except Exception as exc:
        # FTS5 can raise OperationalError on syntax we did not anticipate
        # (e.g. weird Unicode); swallow and log instead of 500-ing the page.
        log.warning("notes.search.fts_failed", query=q, error=str(exc))
        return []

    return [
        {
            "id": int(row["id"]),
            "snippet": str(row["snippet"]),
            "created_at": str(row["created_at"]) if row["created_at"] is not None else "",
        }
        for row in rows
    ]


@router.get("/notes/search")
async def notes_search_page_redirect(q: str = Query(default="")) -> RedirectResponse:
    """**v1.32**: page moved into /search/everything (notes is a tab there).

    301 preserves any bookmarks. The JSON sibling at
    ``/api/notes/search.json`` stays where it is.
    """
    target = "/search/everything"
    if q.strip():
        from urllib.parse import urlencode  # noqa: PLC0415
        target = "/search/everything?" + urlencode({"q": q.strip()})
    return RedirectResponse(target, status_code=301)


@router.get("/api/notes/search.json")
async def notes_search_json(q: str = Query(default="")) -> JSONResponse:
    """JSON view of the notes search. Same sanitisation + 50-row cap."""
    query = q.strip()
    results: list[dict[str, Any]] = []
    if query:
        async with get_connection() as conn:
            results = await _run_search(conn, query)
        log.info("notes.search.json", query=query, results=len(results))

    return JSONResponse(
        {
            "query": query,
            "results": results,
            "total": len(results),
        }
    )
