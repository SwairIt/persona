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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.routes.share import _sign, _verify
from app.web.templates_engine import templates

router = APIRouter(tags=["share"])
logger = get_logger(__name__)
# v1.3 feature 2/3 — dedicated channel for cover-image lifecycle events
# (admin pick, missing cover, fallback). Kept separate from the generic
# ``logger`` so an operator can grep just the cover-flow noise.
cover_log = get_logger("persona.collection.cover")


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


def _parse_cover_id(raw: str | None, ids: list[int]) -> int | None:
    """Validate the admin-supplied cover id.

    Empty / missing input means "no explicit cover" — the viewer will
    pick its own fallback. Non-integer input is a 400 (the operator
    almost certainly fat-fingered a tag or token). An id that isn't
    actually in the collection is also a 400, because silently pinning
    a cover that the public page can never render would just be a
    delayed bug.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        cover = int(stripped)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cover_shot_id: {stripped!r}",
        ) from exc
    if cover not in ids:
        raise HTTPException(
            status_code=400,
            detail=(
                f"cover_shot_id {cover} is not in the collection's "
                "screenshot_ids list"
            ),
        )
    return cover


@router.post("/api/share/collection")
async def create_collection_share(
    _user: Annotated[SessionRecord, Depends(current_user_required)],
    screenshot_ids: str = Form(...),
    title: str | None = Form(None),
    ttl_hours: int = Form(24),
    cover_shot_id: str | None = Form(None),
) -> dict[str, Any]:
    """Create a shareable collection from a comma-separated id list.

    ``cover_shot_id`` is optional and must reference one of the ids in
    ``screenshot_ids`` (validated in :func:`_parse_cover_id`).
    """
    ids = _parse_ids(screenshot_ids)
    if not ids:
        raise HTTPException(status_code=400, detail="No screenshot ids provided")
    if ttl_hours <= 0:
        raise HTTPException(status_code=400, detail="ttl_hours must be positive")
    cover_id = _parse_cover_id(cover_shot_id, ids)

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
            "INSERT INTO share_collections "
            "(token, title, screenshot_ids, expires_unix, cover_shot_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, title, json.dumps(ids), expires, cover_id),
        )
        await conn.commit()

    logger.info(
        "share_collection_created",
        count=len(ids),
        ttl_hours=ttl_hours,
        title=title,
    )
    if cover_id is not None:
        cover_log.info(
            "share_collection_cover_set",
            token=token,
            cover_shot_id=cover_id,
            count=len(ids),
        )
    return {
        "url": f"/share/collection/{token}",
        "expires_in_hours": ttl_hours,
        "cover_shot_id": cover_id,
    }


@router.get("/share/collection/{token}", response_class=HTMLResponse)
async def view_collection(request: Request, token: str) -> HTMLResponse:
    info = _verify(token)
    if info is None:
        raise HTTPException(status_code=403, detail="Invalid or expired share link")

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT token, title, screenshot_ids, expires_unix, cover_shot_id "
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

        # ``cover_shot_id`` may be ``NULL`` for pre-v1.3 rows, or it may
        # point at a shot that has since been pruned by retention. Both
        # cases degrade to "no hero image" — the gallery below still
        # renders.
        cover_raw = row["cover_shot_id"]
        cover_id: int | None = int(cover_raw) if cover_raw is not None else None
        cover_shot: Any | None = None
        if cover_id is not None:
            cover_shot = next(
                (s for s in shots if int(s["id"]) == cover_id),
                None,
            )
            if cover_shot is None:
                cover_log.warning(
                    "share_collection_cover_missing",
                    token=token,
                    cover_shot_id=cover_id,
                )

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
            "cover_shot": cover_shot,
        },
    )
