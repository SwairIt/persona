"""HTTP routes for per-screenshot sticky-note overlays.

Endpoints:

* ``GET  /api/screenshot/{shot_id}/sticky.json`` — list every sticky for
  a screenshot, oldest first, as a JSON array;
* ``POST /api/screenshot/{shot_id}/sticky``      — create a new sticky
  pinned at ``(x_pct, y_pct)`` with a body and a colour;
* ``POST /api/sticky/{sticky_id}/delete``        — delete a sticky by id.

The fractional coordinates ``x_pct`` / ``y_pct`` live in ``[0.0, 1.0]``
and are clamped server-side — a slightly out-of-range value from a JS
double-click handler is treated as "snap to the edge" rather than a 400,
because that's friendlier for the user and a single pixel of drift is
not worth a hard rejection. Genuinely insane inputs (NaN, infinities,
huge negatives) still raise a 400.

The route layer is deliberately a thin shell over the inline SQL helpers
defined below — there is no separate ``app/storage/sticky_notes.py``
module yet because the surface area is tiny and adding a second file
would only spread the same five queries across two tabs. If the feature
grows (search, history, FTS), the helpers move out cleanly.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.sticky_notes")

router = APIRouter(tags=["sticky_notes"])


_MAX_BODY_LEN = 2000
_MAX_COLOR_LEN = 32
_DEFAULT_COLOR = "yellow"


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "shot_id": int(row["shot_id"]),
        "x_pct": float(row["x_pct"]),
        "y_pct": float(row["y_pct"]),
        "body": str(row["body"]),
        "color": str(row["color"]),
        "created_at": str(row["created_at"]),
    }


def _clamp_unit(value: float, *, field: str) -> float:
    """Clamp ``value`` into ``[0.0, 1.0]``; reject NaN / infinities."""
    if not math.isfinite(value):
        msg = f"{field} must be a finite number in [0, 1]"
        raise HTTPException(status_code=400, detail=msg)
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _normalise_color(raw: str | None) -> str:
    """Strip + cap a colour string, falling back to the table default."""
    text = (raw or "").strip()
    if not text:
        return _DEFAULT_COLOR
    if len(text) > _MAX_COLOR_LEN:
        text = text[:_MAX_COLOR_LEN]
    return text


def _normalise_body(raw: str) -> str:
    """Strip + length-check the sticky body, raising 400 on empty / oversize."""
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="body must not be empty")
    if len(text) > _MAX_BODY_LEN:
        msg = f"body must be <= {_MAX_BODY_LEN} characters"
        raise HTTPException(status_code=400, detail=msg)
    return text


@router.get(
    "/api/screenshot/{shot_id}/sticky.json",
    response_class=JSONResponse,
)
async def list_stickies(shot_id: int) -> JSONResponse:
    """Return every sticky note for the given screenshot, oldest first."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, shot_id, x_pct, y_pct, body, color, created_at "
            "FROM sticky_note "
            "WHERE shot_id = ? "
            "ORDER BY id ASC",
            (shot_id,),
        )
        rows = await cursor.fetchall()
    items: list[dict[str, Any]] = [_row_to_dict(row) for row in rows]
    return JSONResponse(items)


@router.post(
    "/api/screenshot/{shot_id}/sticky",
    response_class=JSONResponse,
)
async def create_sticky(
    shot_id: int,
    x_pct: float = Form(...),
    y_pct: float = Form(...),
    body: str = Form(...),
    color: str = Form(_DEFAULT_COLOR),
) -> JSONResponse:
    """Pin a new sticky note at ``(x_pct, y_pct)`` on the screenshot.

    Coordinates outside ``[0, 1]`` are clamped to the edge; NaN / inf are
    rejected. An empty or whitespace-only body is rejected as 400.
    """
    text = _normalise_body(body)
    swatch = _normalise_color(color)
    x = _clamp_unit(x_pct, field="x_pct")
    y = _clamp_unit(y_pct, field="y_pct")

    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        cursor = await conn.execute(
            "INSERT INTO sticky_note (shot_id, x_pct, y_pct, body, color) "
            "VALUES (?, ?, ?, ?, ?)",
            (shot_id, x, y, text, swatch),
        )
        row_id = cursor.lastrowid
        if row_id is None:
            msg = "INSERT did not return a row id"
            raise RuntimeError(msg)
        await conn.commit()

        cursor = await conn.execute(
            "SELECT id, shot_id, x_pct, y_pct, body, color, created_at "
            "FROM sticky_note WHERE id = ?",
            (row_id,),
        )
        row = await cursor.fetchone()
    if row is None:  # pragma: no cover — sanity guard, INSERT just succeeded
        msg = f"sticky #{row_id} vanished immediately after insert"
        raise RuntimeError(msg)

    created = _row_to_dict(row)
    log.info(
        "sticky_notes.created",
        sticky_id=row_id,
        shot_id=shot_id,
        x_pct=x,
        y_pct=y,
        color=swatch,
    )
    return JSONResponse(created, status_code=201)


@router.post(
    "/api/sticky/{sticky_id}/delete",
    response_class=JSONResponse,
)
async def delete_sticky(sticky_id: int) -> JSONResponse:
    """Delete the sticky; 404 if it never existed (or already gone)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM sticky_note WHERE id = ?",
            (sticky_id,),
        )
        await conn.commit()
    removed = (cursor.rowcount or 0) > 0
    log.info("sticky_notes.deleted", sticky_id=sticky_id, removed=removed)
    if not removed:
        raise HTTPException(status_code=404, detail="Sticky note not found")
    return JSONResponse({"id": sticky_id, "deleted": True})
