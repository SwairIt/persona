"""Search query autocomplete — JSON endpoint for the /search input dropdown.

Combines two sources, both keyed off the user's prefix ``q``:

* ``search_history`` (migration 016) — auto-tracked recent queries, matched as
  a prefix (``query LIKE q || '%'``) so the dropdown narrows as the user types.
* ``saved_search`` (migration 025) — explicit bookmarks, matched anywhere in
  the human title *or* the underlying query so a memorable title surfaces even
  when the user types a fragment of it.

Each suggestion is tagged with ``kind`` (``history`` or ``saved``) so the
frontend can render an icon / badge per source. SQL uses bind parameters and
manual LIKE escaping so a user typing ``%`` or ``_`` cannot turn the prefix
match into a wildcard scan.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.search.autocomplete")

router = APIRouter(tags=["search-autocomplete"])

# Hard cap on returned suggestions — the dropdown shows at most 8 rows and we
# don't want to ship an unbounded list to the browser.
_MAX_SUGGESTIONS = 8
_HISTORY_BUDGET = _MAX_SUGGESTIONS  # request up to N from history first
_SAVED_BUDGET = _MAX_SUGGESTIONS  # then top up from saved bookmarks

# LIKE-pattern metacharacters that must be escaped so a user typing ``%`` does
# not accidentally wildcard-match every row. The escape char is declared
# inline in the SQL (``ESCAPE '\'``).
_LIKE_ESCAPE = "\\"


def _escape_like(raw: str) -> str:
    """Escape SQL LIKE metacharacters in user input.

    Backslash must be escaped first, otherwise it would double-escape the
    sequences inserted in the following replacements.
    """
    return (
        raw.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


@router.get("/api/search/autocomplete", response_class=JSONResponse)
async def search_autocomplete(
    q: str = Query(default="", max_length=200),
) -> JSONResponse:
    """Return up to 8 autocomplete suggestions for the search input.

    Shape::

        {"suggestions": [{"text": "meeting with anna", "kind": "history"}, ...]}

    Empty / whitespace-only ``q`` returns an empty list — the page already
    renders a "Recent" panel for the zero-input case.
    """
    prefix = (q or "").strip()
    if not prefix:
        return JSONResponse({"suggestions": []})

    escaped = _escape_like(prefix)
    prefix_pattern = f"{escaped}%"
    anywhere_pattern = f"%{escaped}%"

    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()

    async with get_connection() as conn:
        history_cursor = await conn.execute(
            "SELECT query FROM search_history "
            "WHERE query LIKE ? ESCAPE '\\' "
            "ORDER BY use_count DESC, last_used_at DESC "
            "LIMIT ?",
            (prefix_pattern, _HISTORY_BUDGET),
        )
        for row in await history_cursor.fetchall():
            text = str(row["query"])
            if text in seen:
                continue
            seen.add(text)
            suggestions.append({"text": text, "kind": "history"})
            if len(suggestions) >= _MAX_SUGGESTIONS:
                break

        if len(suggestions) < _MAX_SUGGESTIONS:
            saved_cursor = await conn.execute(
                "SELECT title, query FROM saved_search "
                "WHERE title LIKE ? ESCAPE '\\' OR query LIKE ? ESCAPE '\\' "
                "ORDER BY created_at DESC "
                "LIMIT ?",
                (anywhere_pattern, anywhere_pattern, _SAVED_BUDGET),
            )
            for row in await saved_cursor.fetchall():
                # Prefer the human title so the dropdown stays readable; fall
                # back to the raw query if title is empty for some reason.
                text = str(row["title"] or row["query"])
                if text in seen:
                    continue
                seen.add(text)
                suggestions.append({"text": text, "kind": "saved"})
                if len(suggestions) >= _MAX_SUGGESTIONS:
                    break

    payload: dict[str, Any] = {"suggestions": suggestions}

    log.debug(
        "search.autocomplete.served",
        prefix=prefix,
        count=len(suggestions),
        history=sum(1 for s in suggestions if s["kind"] == "history"),
        saved=sum(1 for s in suggestions if s["kind"] == "saved"),
    )

    return JSONResponse(payload)
