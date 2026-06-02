"""Shareable collections: bundle N screenshots into one signed link.

Reuses the HMAC machinery from :mod:`app.web.routes.share` so a single
process-local secret signs both single-screenshot and collection tokens.

Collection token payload format: ``collection|{expires_unix}|{nonce}``.
The actual screenshot id list lives in the ``share_collections`` table,
keyed by the full signed token. This keeps URLs short regardless of how
many screenshots a collection contains.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.routes.share import _sign, _verify
from app.web.templates_engine import templates

router = APIRouter(tags=["share"])
logger = get_logger(__name__)


def _parse_ids(raw: str) -> list[int]:
    """Parse a comma-separated id list, dropping blanks and dedup-ing while
    preserving order."""
    seen: set[int] = set()
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            sid = int(chunk)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid screenshot id: {chunk!r}",
            ) from exc
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


@router.post("/api/share/collection")
async def create_collection_share(
    screenshot_ids: str = Form(...),
    title: str | None = Form(None),
    ttl_hours: int = Form(24),
) -> dict[str, Any]:
    """Create a shareable collection from a comma-separated id list."""
    ids = _parse_ids(screenshot_ids)
    if not ids:
        raise HTTPException(status_code=400, detail="No screenshot ids provided")
    if ttl_hours <= 0:
        raise HTTPException(status_code=400, detail="ttl_hours must be positive")

    expires = int(time.time()) + ttl_hours * 3600
    nonce = secrets.token_urlsafe(8)
    payload = f"collection|{expires}|{nonce}"
    token = _sign(payload)

    async with get_connection() as conn:
        # Confirm every id actually exists — fail loudly rather than rendering
        # a half-empty gallery later.
        for sid in ids:
            shot = await get_screenshot(conn, sid)
            if shot is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Screenshot {sid} not found",
                )
        await conn.execute(
            "INSERT INTO share_collections (token, title, screenshot_ids, expires_unix) "
            "VALUES (?, ?, ?, ?)",
            (token, title, json.dumps(ids), expires),
        )
        await conn.commit()

    logger.info(
        "share_collection_created",
        count=len(ids),
        ttl_hours=ttl_hours,
        title=title,
    )
    return {
        "url": f"/share/collection/{token}",
        "expires_in_hours": ttl_hours,
    }


@router.get("/share/collection/{token}", response_class=HTMLResponse)
async def view_collection(request: Request, token: str) -> HTMLResponse:
    info = _verify(token)
    if info is None:
        raise HTTPException(status_code=403, detail="Invalid or expired share link")

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT token, title, screenshot_ids, expires_unix "
            "FROM share_collections WHERE token = ?",
            (token,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Collection not found")

        expires_unix = int(row["expires_unix"])
        if expires_unix < int(time.time()):
            raise HTTPException(status_code=403, detail="Collection expired")

        try:
            ids = json.loads(row["screenshot_ids"])
        except (TypeError, ValueError) as exc:
            logger.error("share_collection_corrupt_ids", token=token, error=str(exc))
            raise HTTPException(status_code=500, detail="Collection data corrupt") from exc

        shots: list[Any] = []
        for sid in ids:
            shot = await get_screenshot(conn, int(sid))
            if shot is not None:
                shots.append(shot)

    title = row["title"] or f"Shared collection ({len(shots)} screenshots)"
    hours_left = max(0, (expires_unix - int(time.time())) // 3600)

    return templates.TemplateResponse(
        request,
        "shared_collection.html",
        {
            "title": title,
            "active_nav": "",
            "collection_title": row["title"],
            "shots": shots,
            "count": len(shots),
            "expires_unix": expires_unix,
            "hours_left": hours_left,
            "token": token,
        },
    )
