"""Recycle bin UI — list soft-deleted rows, restore them, or purge early.

* ``GET  /recycle``               renders the bin table.
* ``POST /recycle/{id}/restore``  re-inserts the row into its original table.
* ``POST /recycle/{id}/purge``    hard-deletes the row right now (no wait).

The settings cog at the top of every page links here; the retention
worker handles the time-based purge automatically once a row crosses
``settings.recycle_retention_days``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.recycle import list_bin, purge_expired, restore
from app.settings import get_settings
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["recycle"])
log = get_logger("persona.web.recycle")

# Bin listing cap — high enough to expose a reasonable backlog without
# letting a request page through tens of thousands of rows at once.
_LIST_LIMIT = 200


@router.get("/recycle", response_class=HTMLResponse)
async def recycle_page(request: Request) -> HTMLResponse:
    """Render the recycle bin table."""
    settings = get_settings()
    entries = await list_bin(limit=_LIST_LIMIT)
    return templates.TemplateResponse(
        request,
        "recycle.html",
        {
            "title": "Recycle bin",
            "active_nav": "settings",
            "entries": entries,
            "retention_days": settings.recycle_retention_days,
        },
    )


@router.post("/recycle/{recycle_id}/restore")
async def recycle_restore(recycle_id: int) -> RedirectResponse:
    """Pull one row back out of the bin into its original table."""
    ok = await restore(recycle_id)
    if not ok:
        await log_action(
            "recycle.restore",
            target=str(recycle_id),
            detail="not found",
            success=False,
        )
        raise HTTPException(status_code=404, detail="Recycle bin entry not found")
    await log_action("recycle.restore", target=str(recycle_id))
    return RedirectResponse(url="/recycle", status_code=303)


@router.post("/recycle/{recycle_id}/purge")
async def recycle_purge(recycle_id: int) -> RedirectResponse:
    """Hard-delete one row from the bin right now, skipping the wait.

    Implementation reuses :func:`app.recycle.purge_expired` by first
    nudging this row's ``deleted_at`` into the far past, then asking
    purge_expired to do the actual unlink + delete. That keeps the
    "purge a file from disk" code in exactly one place.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM recycle_bin WHERE id = ?",
            (recycle_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await log_action(
                "recycle.purge",
                target=str(recycle_id),
                detail="not found",
                success=False,
            )
            raise HTTPException(status_code=404, detail="Recycle bin entry not found")
        ancient = (datetime.now(UTC) - timedelta(days=3650)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        await conn.execute(
            "UPDATE recycle_bin SET deleted_at = ? WHERE id = ?",
            (ancient, recycle_id),
        )
        await conn.commit()

    purged = await purge_expired(retention_days=1)
    await log_action(
        "recycle.purge",
        target=str(recycle_id),
        detail=f"purged_in_batch={purged}",
    )
    return RedirectResponse(url="/recycle", status_code=303)
