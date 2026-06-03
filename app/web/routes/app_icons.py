"""HTTP surface for the per-app icon cache.

Endpoints:

* ``GET /app-icon/{app_name}.png`` — serve the cached PNG (or generate
  + cache on first hit) via :mod:`app.app_icons`. The response carries
  a long ``Cache-Control`` max-age so a busy timeline page does not
  slam the cache table on every scroll — icons rarely change, and when
  they do the caller invalidates explicitly via
  :func:`app.app_icons.invalidate` (or the v0.58 admin reset below).
* ``POST /app-icon/{app_name}/upload`` *(v0.58)* — accept a multipart
  PNG upload and persist it with ``source='user'`` so the operator can
  override an ugly auto-generated tile for an app that does not surface
  a real exe icon (web apps wrapped in Electron, in-house tooling,
  remote-desktop windows). Validates PNG magic bytes, decoded image
  dimensions and a hard byte ceiling *before* hitting the DB.
* ``DELETE /app-icon/{app_name}/reset`` *(v0.58)* — drop the cached row
  so the next ``GET`` regenerates from Shell32 / initials. Idempotent.

URL-decoding is left to FastAPI's path parameter machinery, which
already runs ``urllib.parse.unquote`` on incoming segments — so a
template that emits ``/app-icon/Visual%20Studio%20Code.png`` reaches us
as ``app_name="Visual Studio Code"`` without explicit decoding here.
A short defensive ``unquote`` is still applied for double-encoded URLs
(``%2520``) that bookmarklets or external callers occasionally produce.
"""

from __future__ import annotations

import io
from typing import Annotated, Final
from urllib.parse import unquote

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from PIL import Image, UnidentifiedImageError

from app.app_icons import get_icon_png, invalidate, store_user_icon
from app.logging_setup import get_logger

log = get_logger("persona.app_icons.upload")

router = APIRouter(tags=["app-icons"])

# 24 hours. Long enough that a re-rendered timeline page reuses the
# browser cache; short enough that a manual ``invalidate`` call propagates
# within a day even without a cache-busting query param.
_MAX_AGE_SECONDS: Final[int] = 86_400

# Cap on the path-encoded ``app_name``. SQLite would happily store
# multi-megabyte keys, but a 200-char ceiling shields us from a misbehaving
# client trying to fill the cache table with garbage rows.
_MAX_APP_NAME_LEN: Final[int] = 200

# Upload ceiling. 256 KB is roomy for a 256x256 RGBA PNG (a hand-drawn
# logo compresses to ~30-80 KB in practice) and small enough that a
# malicious client cannot fill the SQLite blob page cache with a single
# request. We refuse *before* decoding so PIL never sees the oversize
# buffer.
_MAX_UPLOAD_BYTES: Final[int] = 256 * 1024

# Max decoded dimension. We enforce both width and height so a 1x65536
# strip (which would compress fine yet rasterise to garbage) is refused.
_MAX_PIXEL_DIMENSION: Final[int] = 256

# PNG magic-bytes prefix. Used as a fast guard before handing the
# buffer to PIL — a JPEG / WebP / GIF would be rejected here without
# spinning up the decoder. See RFC 2083 §3.1.
_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"


def _normalise_path_app_name(app_name: str) -> str:
    """Decode, trim and bound-check a path-segment ``app_name``.

    Centralised so the three handlers below behave identically on edge
    cases (double-encoding, whitespace, length overflow). Raises an
    HTTP 400 with a precise reason rather than a vague "invalid input"
    so an operator hitting the upload form sees actionable feedback.
    """
    decoded = unquote(app_name).strip()
    if not decoded:
        raise HTTPException(status_code=400, detail="Empty app_name")
    if len(decoded) > _MAX_APP_NAME_LEN:
        raise HTTPException(status_code=400, detail="app_name too long")
    return decoded


def _validate_png_bytes(raw: bytes) -> None:
    """Reject anything that is not a plausible small PNG.

    Three layered checks, fastest first:

    1. Byte ceiling — refused before PIL is invoked.
    2. PNG magic prefix — catches JPEG/WebP/GIF without paying the
       decoder cost.
    3. PIL ``Image.open`` + ``verify`` + dimension check — catches
       corrupt PNGs and oversized canvases.

    All failures raise :class:`fastapi.HTTPException` with a 400 status
    and a one-line detail; the caller never has to translate.
    """
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")
    if not raw.startswith(_PNG_MAGIC):
        raise HTTPException(status_code=400, detail="not a PNG (magic bytes mismatch)")

    # ``verify`` walks the chunk structure but invalidates the image
    # object — we reopen for the dimension probe. Both calls are bounded
    # to the in-memory buffer, no disk IO.
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(raw)) as decoded:
            width, height = decoded.size
            fmt = decoded.format
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid PNG: {exc}") from exc

    if fmt != "PNG":
        raise HTTPException(status_code=400, detail=f"expected PNG, got {fmt}")
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="PNG has zero dimension")
    if width > _MAX_PIXEL_DIMENSION or height > _MAX_PIXEL_DIMENSION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"PNG too large ({width}x{height}); "
                f"max {_MAX_PIXEL_DIMENSION}x{_MAX_PIXEL_DIMENSION}"
            ),
        )


@router.get("/app-icon/{app_name}.png")
async def app_icon(app_name: str) -> Response:
    """Return the cached PNG for ``app_name`` (or generate it on miss).

    ``app_name`` is the Win32 window-class / executable name the capture
    loop records on every screenshot. We never 404 here — the fallback
    initials tile means *every* string yields a valid PNG, which keeps
    the timeline rendering robust against newly-seen apps that have not
    been pre-cached yet.
    """
    decoded = _normalise_path_app_name(app_name)
    png_bytes = await get_icon_png(decoded)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": f"public, max-age={_MAX_AGE_SECONDS}, immutable"},
    )


def _wants_html(request: Request) -> bool:
    """Return True when the caller is a browser form, not a JSON client.

    Forms post ``multipart/form-data`` and accept ``text/html``; the
    future JS uploader will send ``Accept: application/json``. We
    differentiate so the same endpoint redirects the admin page back
    to itself (post-redirect-get) *and* returns 204 for the JSON
    client without forcing two near-identical routes.
    """
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


@router.post("/app-icon/{app_name}/upload")
async def app_icon_upload(
    request: Request,
    app_name: str,
    file: Annotated[UploadFile, File(...)],
) -> Response:
    """Persist an operator-uploaded PNG as the icon for ``app_name``.

    Validation chain: bound the byte count, check PNG magic, decode
    with PIL, enforce 256x256 ceiling. On success the row is written
    with ``source='user'`` and the response is either a 303 redirect
    back to the admin page (browser form post) or a 204 No Content
    (JSON client) — see :func:`_wants_html`.
    """
    decoded = _normalise_path_app_name(app_name)
    raw = await file.read()
    _validate_png_bytes(raw)

    await store_user_icon(decoded, raw)
    log.info(
        "app_icons.uploaded",
        app_name=decoded,
        bytes=len(raw),
        content_type=file.content_type,
        filename=file.filename,
    )
    if _wants_html(request):
        return RedirectResponse(url="/settings/app-icons", status_code=303)
    return Response(status_code=204)


@router.post("/app-icon/{app_name}/reset")
@router.delete("/app-icon/{app_name}/reset")
async def app_icon_reset(request: Request, app_name: str) -> Response:
    """Drop the cached row for ``app_name`` so auto-generation runs again.

    Idempotent: resetting an app that has no cached row is *not* an
    error. We accept ``POST`` *and* ``DELETE`` on the same path so a
    plain HTML form (no JS) can trigger the reset via ``method="post"``
    while the JSON API stays REST-clean. We still log the event so an
    operator audit trail of "who reverted which override" exists.
    """
    decoded = _normalise_path_app_name(app_name)
    await invalidate(decoded)
    log.info("app_icons.reset", app_name=decoded)
    if _wants_html(request):
        return RedirectResponse(url="/settings/app-icons", status_code=303)
    return Response(status_code=204)


__all__ = ["router"]
