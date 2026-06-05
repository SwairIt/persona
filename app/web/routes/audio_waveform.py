"""HTTP surface for pre-rendered audio waveform thumbnails.

Two endpoints, both anchored on an ``audio_segment`` row id:

* ``GET  /api/audio-segment/{segment_id}/waveform.svg`` — serves the
  cached SVG markup as ``image/svg+xml``. Falls back to a 1-bar
  placeholder SVG (still 200 OK) when the row has not been pre-rendered
  yet so the template's ``<img>`` does not flash a broken icon. A 404
  is returned only for genuinely missing rows.
* ``POST /api/audio-segment/{segment_id}/waveform/regenerate`` — triggers
  a one-off :func:`app.audio_waveform.generate_waveform` call for the
  row, bypassing the worker's poll cadence. Returns the renderer's
  result dict as JSON so the operator can see the resolved
  ``status`` / ``svg_length`` directly from the network panel.

The module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — the task spec forbids touching ``main.py``.
Wire it up with::

    from app.web.routes import audio_waveform as audio_waveform_routes
    app.include_router(audio_waveform_routes.router)

No list / day-view templates are modified by this feature — the SVG
column sits in the DB ready for a future template to pick up via
``{{ seg.waveform_svg|safe }}``.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from app.audio_waveform import WaveformResult, generate_waveform
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.audio_waveform.web")

router = APIRouter(tags=["audio-waveform"])


# A minimal placeholder SVG returned when the row exists but its
# ``waveform_svg`` column has not been populated yet. Same canvas as
# the default render (240 x 24 = 60 bars * 4 px) so a host template
# that reserves space for the thumbnail keeps the same layout while
# the worker catches up. ``currentColor`` lets the template control
# the colour via CSS.
_PLACEHOLDER_SVG: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'viewBox="0 0 240 24" width="240" height="24" '
    'preserveAspectRatio="none" role="img" '
    'aria-label="audio waveform pending">'
    '<rect x="0" y="11.5" width="240" height="1" '
    'fill="currentColor" opacity="0.4"/>'
    "</svg>"
)


async def _load_cached_svg(segment_id: int) -> tuple[bool, str | None]:
    """Return ``(row_exists, waveform_svg)`` for the row.

    Parametrised SQL — the only user-controlled value (``segment_id``)
    is bound, never interpolated. ``row_exists`` is the first member
    so callers can distinguish "no row" (→ 404) from "row with NULL
    waveform" (→ placeholder).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT waveform_svg FROM audio_segment WHERE id = ?",
            (int(segment_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return False, None
    raw_svg = row["waveform_svg"]
    if raw_svg is None:
        return True, None
    text = str(raw_svg)
    return True, text or None


@router.get("/api/audio-segment/{segment_id}/waveform.svg")
async def waveform_svg(segment_id: int) -> Response:
    """Serve the cached SVG waveform thumbnail for ``segment_id``.

    Returns ``200`` with the cached markup when the row has been
    pre-rendered, ``200`` with a 1-bar placeholder when the row
    exists but is still NULL (the worker has not caught up yet), and
    ``404`` only for genuinely missing rows. The two-tier 200 surface
    keeps the host ``<img>`` element from flashing a broken-image
    icon during the brief window between row insert and worker tick.

    A short private cache header lets the browser reuse the SVG
    during a single page render without pinning a stale render after
    the operator hits the regenerate endpoint.
    """
    exists, cached = await _load_cached_svg(segment_id)
    if not exists:
        log.info("audio_waveform.web.not_found", segment_id=segment_id)
        raise HTTPException(status_code=404, detail="not found")
    body = cached if cached is not None else _PLACEHOLDER_SVG
    log.info(
        "audio_waveform.web.served",
        segment_id=segment_id,
        cached=cached is not None,
        bytes=len(body),
    )
    return Response(
        content=body,
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.post("/api/audio-segment/{segment_id}/waveform/regenerate")
async def waveform_regenerate(segment_id: int) -> JSONResponse:
    """Trigger a one-off waveform regeneration for ``segment_id``.

    The renderer is idempotent — calling this on an already-rendered
    row just overwrites the existing ``waveform_svg`` with a fresh
    render (useful after switching bar heights, fixing a decoder
    install, or recovering a previously-purged file).

    Returns the renderer's result dict verbatim as JSON. Surfaces a
    ``404`` only when the row genuinely does not exist — the
    ``missing`` / ``error`` renderer statuses still come back as
    ``200`` with the discriminator in the body so the operator can
    distinguish "row absent" from "file unreadable".
    """
    # Pre-flight: the renderer would also report ``missing`` for an
    # absent row, but we want to surface a 404 to the HTTP layer so a
    # mis-typed id fails loudly rather than silently returning an
    # error status with a 200.
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM audio_segment WHERE id = ?",
            (int(segment_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        log.info("audio_waveform.regenerate.not_found", segment_id=segment_id)
        raise HTTPException(status_code=404, detail="not found")

    result: WaveformResult = await generate_waveform(int(segment_id))
    log.info(
        "audio_waveform.regenerate.done",
        segment_id=segment_id,
        status=result.get("status"),
        svg_length=result.get("svg_length"),
    )
    return JSONResponse(dict(result))


__all__ = ["router"]
