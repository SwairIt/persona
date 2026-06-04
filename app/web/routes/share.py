"""Time-limited signed share links for individual screenshots.

**v1.28 — deprecation status.** The POST writer endpoint
``/api/screenshots/{id}/share`` is **removed** (returns 410 Gone) — use
:mod:`app.web.routes.shot_share` (``POST
/api/screenshot/{id}/share/create``) instead. That newer endpoint
stores a DB row + supports revocation; this one minted opaque HMAC
tokens that could not be invalidated until they expired.

The HMAC primitives (``_secret`` / ``_sign`` / ``_verify`` /
``create_share_token``) stay public because ``shot_share.py``,
``share_collection.py`` and ``recycle.py`` reuse them. Likewise the
read endpoint ``GET /share/{token}`` stays alive so any pre-v1.28
link a user emailed continues to work until it naturally expires.

Token format: ``base64(payload).hex(hmac_sha256(secret, payload))``
where ``payload = "{screenshot_id}|{expires_unix}|{purpose}"``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["share"])


def _secret() -> bytes:
    """Process-local share secret. Persisted under data/.share_secret."""
    settings = get_settings()
    path = settings.data_dir / ".share_secret"
    if path.exists():
        return path.read_bytes()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32)
    path.write_bytes(secret)
    return secret


def _sign(payload: str) -> str:
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=") + "." + sig


def _verify(token: str) -> dict | None:
    try:
        encoded, sig = token.split(".", 1)
    except ValueError:
        return None
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(expected, sig):
        return None
    parts = payload.split("|")
    if len(parts) != 3:
        return None
    try:
        sid = int(parts[0])
        expires = int(parts[1])
    except ValueError:
        return None
    if expires < int(time.time()):
        return None
    return {"screenshot_id": sid, "expires": expires, "purpose": parts[2]}


def create_share_token(screenshot_id: int, *, ttl_hours: int = 24, purpose: str = "view") -> str:
    expires = int(time.time()) + ttl_hours * 3600
    payload = f"{screenshot_id}|{expires}|{purpose}"
    return _sign(payload)


@router.post("/api/screenshots/{screenshot_id}/share")
async def create_share_deprecated(screenshot_id: int, ttl_hours: int = 24) -> dict:
    """**Deprecated since v1.28** — use ``shot_share`` instead.

    Returns 410 Gone with a JSON body that points clients at the new
    endpoint. The old client just sees a hard failure — we deliberately
    do not auto-forward because that masks integrations the operator
    needs to audit and migrate.
    """
    raise HTTPException(
        status_code=410,
        detail={
            "error": "endpoint_deprecated",
            "replaced_by": "POST /api/screenshot/{shot_id}/share/create",
            "since": "v1.28",
            "screenshot_id": screenshot_id,
            "ttl_hours_requested": ttl_hours,
        },
    )


@router.get("/share/{token}", response_class=HTMLResponse)
async def view_shared(request: Request, token: str) -> HTMLResponse:
    info = _verify(token)
    if info is None:
        raise HTTPException(status_code=403, detail="Invalid or expired share link")
    async with get_connection() as conn:
        shot = await get_screenshot(conn, info["screenshot_id"])
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return templates.TemplateResponse(
        request,
        "shared.html",
        {
            "title": f"Shared screenshot #{shot.id}",
            "active_nav": "",
            "shot": shot,
            "token": token,
        },
    )


@router.get("/share/{token}/thumbnail")
async def shared_thumbnail(token: str) -> FileResponse:
    info = _verify(token)
    if info is None:
        raise HTTPException(status_code=403, detail="Invalid or expired share link")
    async with get_connection() as conn:
        shot = await get_screenshot(conn, info["screenshot_id"])
    if shot is None or not shot.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumbnail unavailable")
    path = Path(shot.thumbnail_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file missing")
    return FileResponse(path, media_type="image/webp")
