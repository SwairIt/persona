"""HTTP surface for the per-app icon cache.

A single route, ``GET /app-icon/{app_name}.png``, that serves the cached
PNG (or generates + caches one on first hit) produced by
:mod:`app.app_icons`. The response carries a long ``Cache-Control``
max-age so a busy timeline page does not slam the cache table on every
scroll — the icon for a given app rarely changes, and when it does the
caller invalidates explicitly via :func:`app.app_icons.invalidate`.

URL-decoding is left to FastAPI's path parameter machinery, which
already runs ``urllib.parse.unquote`` on incoming segments — so a
template that emits ``/app-icon/Visual%20Studio%20Code.png`` reaches us
as ``app_name="Visual Studio Code"`` without explicit decoding here.
A short defensive ``unquote`` is still applied for double-encoded URLs
(``%2520``) that bookmarklets or external callers occasionally produce.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.app_icons import get_icon_png
from app.logging_setup import get_logger

log = get_logger("persona.app_icons")

router = APIRouter(tags=["app-icons"])

# 24 hours. Long enough that a re-rendered timeline page reuses the
# browser cache; short enough that a manual ``invalidate`` call propagates
# within a day even without a cache-busting query param.
_MAX_AGE_SECONDS: Final[int] = 86_400

# Cap on the path-encoded ``app_name``. SQLite would happily store
# multi-megabyte keys, but a 200-char ceiling shields us from a misbehaving
# client trying to fill the cache table with garbage rows.
_MAX_APP_NAME_LEN: Final[int] = 200


@router.get("/app-icon/{app_name}.png")
async def app_icon(app_name: str) -> Response:
    """Return the cached PNG for ``app_name`` (or generate it on miss).

    ``app_name`` is the Win32 window-class / executable name the capture
    loop records on every screenshot. We never 404 here — the fallback
    initials tile means *every* string yields a valid PNG, which keeps
    the timeline rendering robust against newly-seen apps that have not
    been pre-cached yet.
    """
    decoded = unquote(app_name).strip()
    if not decoded:
        raise HTTPException(status_code=400, detail="Empty app_name")
    if len(decoded) > _MAX_APP_NAME_LEN:
        raise HTTPException(status_code=400, detail="app_name too long")

    png_bytes = await get_icon_png(decoded)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": f"public, max-age={_MAX_AGE_SECONDS}, immutable"},
    )


__all__ = ["router"]
