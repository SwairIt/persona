"""Admin-side UI for managing per-screenshot share links (v0.43).

This module only renders the dashboard a Persona operator sees. The
public viewer page and the JSON-issuing/revoking endpoints live in
:mod:`app.web.routes.shot_share`.

We intentionally do not list issued tokens here: the share module is
stateless and never persists the tokens it signs. The "existing tokens"
section therefore surfaces the *revoke history* pulled out of the
``shot_share_revoked`` ``kv_settings`` row — the most actionable piece of
state we actually keep. The revoke button is the global kill switch
described in :mod:`app.web.routes.shot_share`.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.routes.shot_share import _load_revoked
from app.web.templates_engine import templates

router = APIRouter(tags=["shot-share-ui"])
logger = get_logger("persona.shot_share")


@router.get("/screenshot/{screenshot_id}/share", response_class=HTMLResponse)
async def shot_share_admin(request: Request, screenshot_id: int) -> HTMLResponse:
    """Render the admin page with the create-link form and revoke history."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        revoked_entries: list[dict[str, int]] = await _load_revoked(conn)

    history: list[dict[str, Any]] = [
        {"revoked_at": entry["revoked_at"]}
        for entry in revoked_entries
        if entry["id"] == screenshot_id
    ]
    history.sort(key=lambda row: row["revoked_at"], reverse=True)

    return templates.TemplateResponse(
        request,
        "shot_share_ui.html",
        {
            "title": f"Share screenshot #{screenshot_id}",
            "active_nav": "timeline",
            "shot": shot,
            "revoke_history": history,
            "now": int(time.time()),
        },
    )
