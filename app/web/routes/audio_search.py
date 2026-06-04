"""Transcript search over ``audio_segment.transcript`` rows.

v1.11 feature 3/3, route 4 of 5. The transcript column is filled in
out-of-band by the Whisper worker (v1.11 feature 2/3), so the search
surface is a plain ``LIKE %q%`` rather than FTS5: most rows ship with
zero or one sentence and the data set is small enough that a full
table scan is fine. When the corpus eventually warrants FTS5 we'll
back this endpoint with the same ``notes_fts``-style triggers used by
:mod:`app.web.routes.notes_search`.

Both query forms are exposed:

* ``GET /audio/search?q=TERM`` — HTML page.
* ``GET /api/audio/search.json?q=TERM`` — machine-readable companion.

The endpoint always returns HTTP 200, even on empty / no-match input;
the template renders an explicit "type something" / "no hits" empty
state in those branches. Results are capped at
:data:`_MAX_RESULTS` and ordered ``captured_at DESC`` — newest first
matches the timeline's expectation that "recent" outranks "old"
within the same relevance class.

This module deliberately does NOT register itself with the FastAPI
app in :mod:`app.web.main` — the task spec forbids touching
``main.py``. Wire it up with::

    from app.web.routes import audio_search as audio_search_routes
    app.include_router(audio_search_routes.router)
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.audio.web")

router = APIRouter(tags=["audio-search"])

# Hard cap on rows returned for a single query. A power-user with a
# year of dictated voice memos may have hundreds of hits for a common
# word; 200 is comfortably more than what fits on one page and stays
# under the "ten thousand DOM nodes" usability cliff.
_MAX_RESULTS: Final[int] = 200

# SQLite LIKE metacharacters that, left raw in user input, would turn a
# literal substring match into a wildcard one. We escape them with a
# ``\`` so ``"100%"`` searches for the literal string instead of "any
# row containing 100". The ``ESCAPE '\'`` clause in the SQL below
# pairs with this map.
_LIKE_METACHARS: Final[tuple[str, ...]] = ("\\", "%", "_")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_like(term: str) -> str:
    """Escape SQLite LIKE wildcards in ``term`` against ``ESCAPE '\\'``.

    Order matters — the backslash escape must run *first* so the later
    substitutions don't double-escape the ``\\%`` / ``\\_`` they emit.
    """
    out = term
    for char in _LIKE_METACHARS:
        out = out.replace(char, "\\" + char)
    return out


def _snippet(transcript: str, query: str, window: int = 80) -> str:
    """Return a ``…before<match>after…`` snippet around the first hit.

    Centres the snippet on the first occurrence of ``query`` (case-
    insensitive) so the operator sees the matched phrase rather than
    the row's opening words. Falls back to the leading ``window * 2``
    chars when the query somehow isn't in the transcript (defensive —
    the LIKE filter should have screened those rows out, but a
    sufficiently weird Unicode normalisation difference is conceivable).
    """
    if not transcript:
        return ""
    hay = transcript.lower()
    needle = query.lower()
    if not needle:
        # Empty query never matches anything — the route layer guards
        # against this but keep the helper total.
        return transcript[: window * 2]
    idx = hay.find(needle)
    if idx < 0:
        return transcript[: window * 2]
    start = max(0, idx - window)
    end = min(len(transcript), idx + len(needle) + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(transcript) else ""
    return f"{prefix}{transcript[start:end]}{suffix}"


async def _run_search(query: str) -> list[dict[str, Any]]:
    """Execute the parametrised LIKE search, return projected rows.

    SQL is parametrised — the user input never goes through string
    formatting. Wildcards are added by the *query builder*, not the
    user; user-supplied ``%`` / ``_`` are escaped via
    :func:`_escape_like` so they search literally.
    """
    if not query:
        return []
    escaped = _escape_like(query)
    pattern = f"%{escaped}%"
    async with get_connection() as conn:
        cursor = await conn.execute(
            r"""
            SELECT id,
                   captured_at,
                   duration_seconds,
                   codec,
                   bitrate,
                   size_bytes,
                   path,
                   transcript
              FROM audio_segment
             WHERE transcript IS NOT NULL
               AND transcript != ''
               AND transcript LIKE ? ESCAPE '\'
             ORDER BY captured_at DESC, id DESC
             LIMIT ?
            """,
            (pattern, _MAX_RESULTS),
        )
        rows = await cursor.fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        transcript = "" if row["transcript"] is None else str(row["transcript"])
        stored_path = row["path"]
        has_audio = bool(stored_path is not None and str(stored_path).strip() != "")
        results.append(
            {
                "id": int(row["id"]),
                "captured_at": str(row["captured_at"]),
                "duration_seconds": float(row["duration_seconds"] or 0.0),
                "codec": str(row["codec"] or ""),
                "bitrate": int(row["bitrate"] or 0),
                "size_bytes": int(row["size_bytes"] or 0),
                "transcript": transcript,
                "snippet": _snippet(transcript, query),
                "has_audio": has_audio,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/audio/search")
async def audio_search_page_redirect(q: str = Query(default="")) -> RedirectResponse:
    """**v1.32**: page moved into the unified /search/everything view.

    Audio is now the 6th tab there (v1.29). 301-redirect preserves any
    bookmarks. The JSON sibling at ``/api/audio/search.json`` stays
    where it is for any external scripts that may depend on it.
    """
    target = "/search/everything"
    if q.strip():
        from urllib.parse import urlencode  # noqa: PLC0415
        target = "/search/everything?" + urlencode({"q": q.strip()})
    return RedirectResponse(target, status_code=301)


@router.get("/api/audio/search.json", response_class=JSONResponse)
async def audio_search_json(q: str = Query(default="")) -> JSONResponse:
    """JSON companion to :func:`audio_search_page`."""
    query = q.strip()
    results: list[dict[str, Any]] = []
    if query:
        results = await _run_search(query)
        log.info("audio.search.json", query=query, results=len(results))

    return JSONResponse(
        {
            "query": query,
            "results": results,
            "total": len(results),
            "truncated": len(results) >= _MAX_RESULTS,
        }
    )


__all__ = ["router"]
