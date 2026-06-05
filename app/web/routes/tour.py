"""First-launch interactive product tour — 5 step overlay onboarding.

After the setup wizard finishes, brand-new installs land on ``/tour``
once. The tour walks through Timeline → Ask → Memory → Capture
settings → LLM setup with a numbered overlay, two buttons (Next/Skip)
and a final "Finish" step. Completion is tracked via a single
``tour_completed`` kv row so the redirect from ``setup.py`` only fires
on the very first visit.

The tour deliberately lives outside :func:`render_with_base` — it
uses a fullscreen custom layout rather than ``base.html`` so the user
is not distracted by the standard chrome (nav, status pill, footer)
while reading the explanations.

API surface:

    * ``GET  /tour``            — renders the overlay (HTML).
    * ``GET  /api/tour/seen``   — returns ``{"seen": bool}``.
    * ``POST /api/tour/seen``   — marks the tour as completed.
    * ``POST /api/tour/skip``   — same effect as ``/seen`` but logged
                                  separately so we can later tell
                                  "Finish" apart from "Skip" in
                                  product analytics.

Both POSTs are idempotent: the underlying ``set_kv`` is an upsert so
double-clicks or browser retries can never wedge the flag.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["tour"])
log = get_logger("persona.tour")


# kv_settings row name. Centralised so :mod:`app.web.routes.setup`
# (and any future export hooks) can re-import without grepping for
# string literals.
KV_TOUR_COMPLETED: Final[str] = "tour_completed"


async def is_tour_completed() -> bool:
    """Return ``True`` iff the user has finished or skipped the tour.

    Mirrors :func:`app.web.routes.setup.is_setup_complete`: any value
    other than the literal ``"1"`` is treated as "not done", including
    ``None`` (no row yet) and any legacy serialisation. We use ``"1"``
    rather than ``"true"`` here because the rest of the kv namespace
    is mixed — and ``"1"`` is what every other flag-style row in the
    codebase already writes (e.g. ``mic_paused``, ``digest_enabled``).
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, KV_TOUR_COMPLETED)
    return raw == "1"


async def _mark_completed() -> None:
    """Persist the ``tour_completed`` flag using a parametrised upsert.

    Wrapped in its own helper so the two POST routes share exactly one
    code path — no copy/paste of the SQL or the logging shape.
    """
    async with get_connection() as conn:
        await set_kv(conn, KV_TOUR_COMPLETED, "1")


@router.get("/tour", response_class=HTMLResponse)
async def tour_page(request: Request) -> HTMLResponse:
    """Render the 5-step fullscreen overlay.

    The template does *not* extend ``base.html`` — see module docstring
    for why. ``active_nav`` is still passed in so a future revision can
    re-introduce the standard chrome without touching the route.
    """
    return templates.TemplateResponse(
        request,
        "tour.html",
        {
            "title": "Tour",
            "active_nav": "timeline",
        },
    )


@router.get("/api/tour/seen", response_class=JSONResponse)
async def tour_seen_get() -> JSONResponse:
    """Report whether the tour has been completed at least once."""
    seen = await is_tour_completed()
    return JSONResponse({"seen": seen})


@router.post("/api/tour/seen", response_class=JSONResponse)
async def tour_seen_post() -> JSONResponse:
    """Mark the tour as completed via the "Finish" button on step 5."""
    await _mark_completed()
    log.info("tour.completed", reason="finish")
    return JSONResponse({"seen": True})


@router.post("/api/tour/skip", response_class=JSONResponse)
async def tour_skip_post() -> JSONResponse:
    """Mark the tour as completed via the "Skip" button.

    Same kv write as :func:`tour_seen_post` but logged under a
    different ``reason`` so we can later tell the two paths apart in
    structured-log aggregation.
    """
    await _mark_completed()
    log.info("tour.completed", reason="skip")
    return JSONResponse({"seen": True})
