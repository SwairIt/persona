"""HTTP endpoint that streams QR-code PNGs for share links (v0.54).

The single route here, :func:`qr_png`, exists so the v0.43 share-link
admin template can drop in an ``<img src="/api/qr.png?text=...">`` next
to the URL it generates. The handler keeps three responsibilities:

* **Validate** — refuse anything longer than 1024 characters. A QR code
  big enough to encode that is already unreadable by phone cameras, and
  the cap also caps the CPU spent inside :func:`make_qr_png`.
* **Offload** — encoding is synchronous CPU work, so we hand it to a
  worker thread via :func:`anyio.to_thread.run_sync` and never block
  uvicorn's event loop.
* **Cache** — the encoded PNG is a pure function of ``text``, so we set
  a long ``Cache-Control`` and let the browser re-use it instead of
  hammering the encoder on every page render.
"""

from __future__ import annotations

import anyio
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.logging_setup import get_logger
from app.qr import make_qr_png

router = APIRouter(tags=["qr"])
logger = get_logger("persona.qr")

_MAX_TEXT_LEN = 1024


@router.get("/api/qr.png")
async def qr_png(
    text: str = Query(..., description="URL or text to encode."),
) -> Response:
    """Return a PNG QR code encoding ``text``.

    Rejects payloads larger than ``_MAX_TEXT_LEN`` with HTTP 400 so an
    accidental data: URL or pasted multi-kilobyte blob does not turn
    into wasted encoder cycles.
    """
    if len(text) > _MAX_TEXT_LEN:
        logger.warning("qr.text_too_long", text_len=len(text), limit=_MAX_TEXT_LEN)
        raise HTTPException(status_code=400, detail="text too long")

    png_bytes = await anyio.to_thread.run_sync(make_qr_png, text)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )
