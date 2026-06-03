"""Random-screenshot endpoints for serendipitous browsing.

``GET /random`` redirects to ``/screenshot/{id}`` for a uniformly-random
screenshot pulled from the entire history; ``GET /random.json`` returns
the same pick as a small JSON envelope. Both endpoints respond ``404``
when the ``screenshots`` table is empty.

The selection uses ``ORDER BY RANDOM() LIMIT 1``. SQLite scans the table
for this — fine for a personal Persona database, where the cost is
negligible compared to the serendipity payoff and avoids the
``MAX(id) + offset`` corner cases around deleted rows.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.random")

router = APIRouter(tags=["random"])


async def _pick_random_shot() -> dict[str, object] | None:
    """Return ``{id, captured_at, app_name}`` for a random shot, or ``None``.

    Parametrised query (no user input is interpolated, but we keep the
    parametrised-execute habit so the route stays consistent with the
    rest of the codebase).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name "
            "FROM screenshots "
            "ORDER BY RANDOM() "
            "LIMIT 1"
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "captured_at": row["captured_at"],
        "app_name": row["app_name"],
    }


@router.get("/random")
async def random_shot_redirect() -> RedirectResponse:
    """Redirect to ``/screenshot/{id}`` for a uniformly-random screenshot."""
    pick = await _pick_random_shot()
    if pick is None:
        log.info("random_shot_empty")
        raise HTTPException(status_code=404, detail="No screenshots yet")
    shot_id = pick["id"]
    log.info("random_shot_pick", shot_id=shot_id)
    return RedirectResponse(url=f"/screenshot/{shot_id}", status_code=302)


@router.get("/random.json", response_class=JSONResponse)
async def random_shot_json() -> JSONResponse:
    """Return a random shot's id, capture time, and app name as JSON."""
    pick = await _pick_random_shot()
    if pick is None:
        log.info("random_shot_empty")
        raise HTTPException(status_code=404, detail="No screenshots yet")
    log.info("random_shot_pick", shot_id=pick["id"])
    return JSONResponse(pick)
