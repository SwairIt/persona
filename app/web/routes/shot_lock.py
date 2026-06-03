"""HTTP routes for per-shot lock toggling (v0.70).

A "locked" screenshot is one the user has explicitly marked as too
valuable to ever soft-delete via :mod:`app.bulk_delete` or the recycle
flow in :mod:`app.recycle`. The bulk job filters locked rows out (and
reports the skip count); :func:`app.recycle.soft_delete_screenshot`
raises :class:`app.recycle.ShotLocked` rather than silently letting a
locked shot land in the bin. The right-click context menu suppresses
its Delete action when the thumbnail wrapper carries
``data-locked="1"``.

Endpoint contract
-----------------
``POST /api/screenshot/{screenshot_id}/lock`` toggles the row's
``locked`` flag between 0 and 1 and returns ``{"locked": <bool>}``. A
GET on the same URL is intentionally absent — toggling is a state
mutation and must travel as a POST so a stray link prefetch cannot
flip a shot.

Audit + structured logging
--------------------------
Every successful toggle records an :func:`app.audit.log_action` row
under the slug ``shot.lock`` / ``shot.unlock`` with the screenshot id
as the target so a security review can see *who* changed *what*
without ever touching the underlying screenshot payload. The
``persona.shot_lock`` structlog channel mirrors the same fields so the
operator can watch toggles in tail logs without scraping SQLite.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.shot_lock")

router = APIRouter(tags=["shot-lock"])


@router.post("/api/screenshot/{screenshot_id}/lock", response_class=JSONResponse)
async def toggle_lock(screenshot_id: int) -> JSONResponse:
    """Toggle ``screenshots.locked`` between 0 and 1.

    Reads the current value, flips it inside a single transaction, and
    returns the new state as ``{"locked": <bool>}``. Returns 404 when
    the screenshot does not exist so the client can distinguish a
    no-op from a genuine state flip.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT locked FROM screenshots WHERE id = ?",
            (screenshot_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.warning("shot_lock.missing", screenshot_id=screenshot_id)
            raise HTTPException(status_code=404, detail="Screenshot not found")

        current = int(row["locked"] or 0)
        new_value = 0 if current == 1 else 1

        try:
            await conn.execute("BEGIN")
            await conn.execute(
                "UPDATE screenshots SET locked = ? WHERE id = ?",
                (new_value, screenshot_id),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            log.exception("shot_lock.failed", screenshot_id=screenshot_id)
            raise

    locked_bool = new_value == 1
    log.info(
        "shot_lock.toggled",
        screenshot_id=screenshot_id,
        locked=locked_bool,
    )
    await log_action(
        "shot.lock" if locked_bool else "shot.unlock",
        target=str(screenshot_id),
        detail=f"locked={locked_bool}",
    )
    return JSONResponse({"locked": locked_bool})


__all__ = ["router"]
