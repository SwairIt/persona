"""JSON export of every sticky note in the database.

A single endpoint, ``GET /export/sticky-notes.json``, streams the entire
``sticky_note`` table as a JSON array — one object per row, columns
``id``, ``shot_id``, ``x_pct``, ``y_pct``, ``body``, ``color``,
``created_at``. Designed as a portable, scriptable companion to the
per-screenshot ``/api/screenshot/{shot_id}/sticky.json`` endpoint in
:mod:`app.web.routes.sticky_notes`: the latter is for the in-app UI,
this one is the "give me everything" hook used by the CLI's
``export-sticky`` subcommand and any external backup script.

The response sets ``Content-Disposition: attachment`` with a dated
filename so browsers save the file rather than rendering it inline.
The query is parametrised — even though there is no user input here,
keeping the placeholder convention means a future ``since`` / ``until``
filter slots in without rewriting the call.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.sticky_export")

router = APIRouter(tags=["sticky_export"])


_SELECT_ALL_STICKIES = (
    "SELECT id, shot_id, x_pct, y_pct, body, color, created_at "
    "FROM sticky_note "
    "WHERE 1 = ? "
    "ORDER BY id ASC"
)


async def _fetch_all_stickies() -> list[dict[str, Any]]:
    """Materialise every sticky-note row as plain JSON-ready dicts."""
    async with get_connection() as conn:
        cursor = await conn.execute(_SELECT_ALL_STICKIES, (1,))
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "shot_id": int(row["shot_id"]),
            "x_pct": float(row["x_pct"]),
            "y_pct": float(row["y_pct"]),
            "body": str(row["body"]),
            "color": str(row["color"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


@router.get(
    "/export/sticky-notes.json",
    response_class=JSONResponse,
)
async def export_sticky_notes() -> JSONResponse:
    """Stream the full ``sticky_note`` table as a JSON array download."""
    items = await _fetch_all_stickies()
    filename = f"persona-sticky-notes-{date.today().isoformat()}.json"
    log.info("sticky_export.dumped", count=len(items), filename=filename)
    return JSONResponse(
        items,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
