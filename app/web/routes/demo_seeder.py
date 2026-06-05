"""Demo-data seeder UI + JSON API (v1.47).

Surfaces :mod:`app.demo_seeder` over HTTP:

* ``GET  /admin/demo-seeder`` — full-page admin UI with the current
  seeded counts and the Seed / Purge action buttons.
* ``POST /api/demo-seeder/seed`` — body ``{"days": int, "shots_per_day":
  int}``; both fields optional, defaults match
  :func:`app.demo_seeder.seed_demo_data`. Returns the seeder result dict.
* ``POST /api/demo-seeder/purge`` — no body. Returns the per-table
  deletion counts.

The endpoint is intentionally simple: there is no streaming, no
progress channel — seeding ~200 rows finishes in well under a second on
any laptop, so the UI just disables the buttons during the await.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.demo_seeder import (
    SeederRefused,
    count_demo_rows,
    purge_demo_data,
    seed_demo_data,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["demo-seeder"])
log = get_logger("persona.web.demo_seeder")


class SeedRequest(BaseModel):
    """Request body for ``POST /api/demo-seeder/seed``.

    Both fields are bounded so a typo'd payload can't accidentally
    request a billion-row insert. The seeder also clamps them itself
    (defence-in-depth), but rejecting the request at the parse layer is
    cheaper and produces a nicer 422.
    """

    days: int = Field(default=7, ge=1, le=60)
    shots_per_day: int = Field(default=30, ge=1, le=200)


@router.get("/admin/demo-seeder", response_class=HTMLResponse)
async def demo_seeder_page(request: Request) -> HTMLResponse:
    """Render the admin page with the current seeded-row counts."""
    counts = await count_demo_rows()
    return templates.TemplateResponse(
        request,
        "demo_seeder.html",
        {
            "title": "Demo data",
            "active_nav": "settings",
            "counts": counts,
        },
    )


@router.post("/api/demo-seeder/seed")
async def demo_seeder_seed(payload: SeedRequest) -> JSONResponse:
    """Insert demo rows; return the seeder summary dict.

    Returns 409 if the seeder refuses (real data already present) so
    the UI can show the operator a precise reason instead of a generic
    500.
    """
    try:
        result: dict[str, Any] = await seed_demo_data(
            days=payload.days,
            shots_per_day=payload.shots_per_day,
        )
    except SeederRefused as exc:
        log.warning("demo_seeder.api.refused", reason=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(result)


@router.post("/api/demo-seeder/purge")
async def demo_seeder_purge() -> JSONResponse:
    """Delete every demo row across the four affected tables."""
    result = await purge_demo_data()
    return JSONResponse(result)


__all__ = ["router"]
