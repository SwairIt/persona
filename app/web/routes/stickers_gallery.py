"""Cross-shot gallery of every sticky note in the database.

A single HTML endpoint, ``GET /stickers``, renders a responsive grid of
every row in the ``sticky_note`` table (introduced in v0.64), newest
first. Each tile shows:

* the thumbnail of the parent screenshot (so the note stays visually
  anchored to where it was authored),
* a truncated preview of the sticky body (capped at
  :data:`_BODY_PREVIEW_CHARS` so a wall of notes does not turn into a
  wall of text),
* the swatch / colour the user chose for that note as the tile's
  background tint — matching what they see in the in-image overlay.

Clicking a tile jumps to ``/screenshot/{shot_id}`` so the user lands
directly on the canonical shot view, where the same sticky is rendered
in its proper pinned location.

Implementation notes:

* The query is a single LEFT JOIN against ``screenshots`` so that an
  orphaned sticky (shouldn't happen — there is ``ON DELETE CASCADE`` —
  but defence in depth) still renders with a placeholder rather than
  500-ing the whole page.
* SQL is parametrised; the LIMIT is a constant from this module and is
  never user-controlled.
* The route never extends ``base.html`` injection for the sticky body;
  Jinja autoescaping covers the rest. The colour string was already
  length-capped on insert in :mod:`app.web.routes.sticky_notes`, so we
  pass it through unchanged for the ``data-color`` attribute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.stickers_gallery")

router = APIRouter(tags=["stickers_gallery"])

# Hard cap on the number of tiles rendered in one page load. The grid is
# meant for "show me everything I've scribbled" browsing, not infinite
# scroll — a few hundred tiles is plenty for the current single-user
# desktop scope and keeps the initial HTML payload bounded.
_MAX_TILES = 500

# Body preview length, in characters. Notes are capped at 2000 chars on
# insert (see ``_MAX_BODY_LEN`` in :mod:`app.web.routes.sticky_notes`),
# so anything longer than this gets a trailing ellipsis.
_BODY_PREVIEW_CHARS = 120


_SELECT_STICKERS = (
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
    "ORDER BY sn.id DESC "
    "LIMIT ?"
)


def _truncate(text: str, limit: int) -> str:
    """Return ``text`` truncated to ``limit`` chars with a trailing ellipsis."""
    if len(text) <= limit:
        return text
    # Keep an ASCII ellipsis — the template renders inside <p>, so a
    # plain three-dot suffix survives copy-paste better than U+2026.
    return text[: max(0, limit - 3)].rstrip() + "..."


def _row_to_tile(row: aiosqlite.Row) -> dict[str, Any]:
    body_raw = str(row["body"])
    return {
        "sticky_id": int(row["sticky_id"]),
        "shot_id": int(row["shot_id"]),
        "body_full": body_raw,
        "body_preview": _truncate(body_raw, _BODY_PREVIEW_CHARS),
        "color": str(row["color"]),
        "created_at": str(row["created_at"]),
        "thumbnail_path": row["thumbnail_path"],
        "app_name": row["app_name"],
        "window_title": row["window_title"],
        "captured_at": row["captured_at"],
    }


async def _fetch_stickers(limit: int) -> list[dict[str, Any]]:
    """Load up to ``limit`` sticky-note tiles joined to their parent shot."""
    async with get_connection() as conn:
        cursor = await conn.execute(_SELECT_STICKERS, (limit,))
        rows = await cursor.fetchall()
    return [_row_to_tile(row) for row in rows]


@router.get("/stickers", response_class=HTMLResponse)
async def stickers_gallery(request: Request) -> HTMLResponse:
    """Render the cross-shot sticky-notes gallery (newest first, capped)."""
    tiles = await _fetch_stickers(_MAX_TILES)
    log.info(
        "stickers_gallery.rendered",
        tile_count=len(tiles),
        max_tiles=_MAX_TILES,
    )
    return templates.TemplateResponse(
        request,
        "stickers_gallery.html",
        {
            "title": "Stickers",
            "active_nav": "timeline",
            "tiles": tiles,
            "total": len(tiles),
            "max_tiles": _MAX_TILES,
            "body_preview_chars": _BODY_PREVIEW_CHARS,
        },
    )
