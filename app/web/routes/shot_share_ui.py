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
visit_logger = get_logger("persona.share.visits")

# Hard cap on the visit table — the admin view should stay snappy even
# for screenshots that have been hammered. Anything older than the last
# 50 hits is still in the DB; we just don't render it here.
_VISIT_LIMIT = 50


@router.get("/screenshot/{screenshot_id}/share", response_class=HTMLResponse)
async def shot_share_admin(request: Request, screenshot_id: int) -> HTMLResponse:
    """Render the admin page with the create-link form and revoke history."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        revoked_entries: list[dict[str, int]] = await _load_revoked(conn)
        visits = await _recent_visits(conn, screenshot_id)

    history: list[dict[str, Any]] = [
        {"revoked_at": entry["revoked_at"]}
        for entry in revoked_entries
        if entry["id"] == screenshot_id
    ]
    history.sort(key=lambda row: row["revoked_at"], reverse=True)

    visit_logger.debug(
        "shot_share_admin_rendered",
        screenshot_id=screenshot_id,
        visit_rows=len(visits),
    )

    return templates.TemplateResponse(
        request,
        "shot_share_ui.html",
        {
            "title": f"Share screenshot #{screenshot_id}",
            "active_nav": "timeline",
            "shot": shot,
            "revoke_history": history,
            "visits": visits,
            "now": int(time.time()),
        },
    )


async def _recent_visits(conn: Any, screenshot_id: int) -> list[dict[str, Any]]:
    """Return up to :data:`_VISIT_LIMIT` most-recent visit rows for ``shot_id``.

    Parametrised SQL — the ``LIMIT`` constant is interpolated from a module-level
    integer (never user input). ``visited_at`` is the SQLite ``datetime('now')``
    string captured at insert time and is rendered verbatim by the template,
    so we leave it as ``str`` rather than parsing into a ``datetime`` here.
    """
    cursor = await conn.execute(
        """
        SELECT visited_at, ua, ip_prefix
        FROM share_visit
        WHERE shot_id = ?
        ORDER BY visited_at DESC, id DESC
        LIMIT ?
        """,
        (screenshot_id, _VISIT_LIMIT),
    )
    rows = await cursor.fetchall()
    return [
        {
            "visited_at": row["visited_at"],
            "ua": row["ua"],
            "ip_prefix": row["ip_prefix"],
        }
        for row in rows
    ]
