"""HTTP routes for the live JPEG/WebP capture-quality tuner.

Three endpoints:

* ``GET  /settings/capture-quality`` — render the slider + estimator UI.
* ``POST /settings/capture-quality`` — persist the slider value
  (form ``quality: int``), then redirect back to the page.
* ``POST /api/capture-quality/estimate`` — sample N recent thumbnails
  and return JSON with the average re-encoded byte count per probed
  quality band. The Test Sample button calls this and feeds the UI
  table.
* ``GET  /api/capture-quality.json`` — read-only JSON sibling for
  external scripts / dashboards that need to display the current value.

All persistence goes through :mod:`app.capture_quality`; this module
contains zero business logic — it is a thin transport layer.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.capture_quality import (
    DEFAULT_QUALITY,
    PROBE_QUALITIES,
    QUALITY_MAX,
    QUALITY_MIN,
    estimate_size_at_quality,
    get_current_quality,
    set_quality,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["capture-quality"])
log = get_logger("persona.web.capture_quality")


@router.get("/settings/capture-quality", response_class=HTMLResponse)
async def capture_quality_page(request: Request) -> HTMLResponse:
    """Render the slider + estimator panel."""
    current = await get_current_quality()
    return templates.TemplateResponse(
        request,
        "capture_quality.html",
        {
            "title": "Качество скриншотов",
            "active_nav": "settings",
            "current_quality": current,
            "default_quality": DEFAULT_QUALITY,
            "quality_min": QUALITY_MIN,
            "quality_max": QUALITY_MAX,
            "probe_qualities": list(PROBE_QUALITIES),
        },
    )


@router.post("/settings/capture-quality")
async def capture_quality_save(
    quality: Annotated[int, Form()],
) -> RedirectResponse:
    """Persist the slider value (clamped inside :func:`set_quality`)."""
    await set_quality(quality)
    return RedirectResponse(url="/settings/capture-quality", status_code=303)


@router.post("/api/capture-quality/estimate")
async def capture_quality_estimate(
    sample_count: Annotated[int, Form()] = 20,
) -> JSONResponse:
    """Run the byte-savings estimator and return per-band averages.

    Bounded to ``[1, 200]`` to keep a single click from re-encoding the
    entire archive — the estimator opens each file twice and re-encodes
    it five times, so it is cheap-per-sample but unbounded growth is a
    foot-gun.
    """
    clamped = max(1, min(200, int(sample_count)))
    payload = await estimate_size_at_quality(clamped)
    return JSONResponse(payload)


@router.get("/api/capture-quality.json")
async def capture_quality_json() -> JSONResponse:
    """Read-only JSON view of the current setting."""
    current = await get_current_quality()
    return JSONResponse(
        {
            "current_quality": current,
            "default_quality": DEFAULT_QUALITY,
            "min": QUALITY_MIN,
            "max": QUALITY_MAX,
            "probe_qualities": list(PROBE_QUALITIES),
        }
    )


__all__ = ["router"]
