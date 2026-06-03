"""Admin UI for the per-app icon override table.

A single page, ``GET /settings/app-icons``, that lists every app name
the local DB knows about and lets an operator upload a custom 64x64-ish
PNG to replace the auto-generated tile produced by
:mod:`app.app_icons`. The page is read-mostly — the actual upload /
reset POST + DELETE actions live in :mod:`app.web.routes.app_icons` so
the JSON endpoints stay reusable for a future drag-and-drop uploader.

Row sourcing:

* Every row in ``app_icon`` is listed first (sorted by name) so the
  operator can see which apps already have a cached icon and whether
  the source is ``user`` (their override) or auto (``shell32`` /
  ``initials``).
* On top we append the most-captured app names from the ``screenshots``
  table that do *not* yet have a cached row — they will render the
  initials fallback today and are the natural targets for an override.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.app_icons import list_known_icons
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.app_icons.admin")

router = APIRouter(tags=["app-icons"])

# Cap on the "apps without a cached icon row" sidecar. The screenshots
# table on a long-running install can contain tens of thousands of
# distinct ``app_name`` values; we only ever need the top handful for
# the operator to start customising.
_SUGGESTION_LIMIT: Final[int] = 24
_TOP_APPS_LIMIT: Final[int] = 128


@router.get("/settings/app-icons", response_class=HTMLResponse)
async def app_icons_admin_page(request: Request) -> HTMLResponse:
    """Render the per-app icon override admin page.

    Two slices in one template:

    * ``items`` — every ``app_icon`` row, sorted by ``app_name`` so the
      list is stable between renders and the operator can scan for a
      specific app with browser-find.
    * ``suggested`` — the most-screenshotted apps that have *no*
      cached row yet, so the operator can pre-emptively upload a tile
      before the first auto-generate writes an ``initials`` row.
    """
    items = await list_known_icons()
    existing = {item["app_name"] for item in items}

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC LIMIT ?",
            (_TOP_APPS_LIMIT,),
        )
        rows = await cursor.fetchall()

    # ``app_icon`` keys are normalised (lowercased + trimmed) — see
    # ``app.app_icons._normalise_key`` — while ``screenshots.app_name``
    # is stored as-captured. We compare on the normalised form so a
    # cached row for ``slack`` does not show ``Slack`` as "missing".
    suggested = [
        {"app_name": str(row["app_name"]), "count": int(row["n"])}
        for row in rows
        if str(row["app_name"]).strip().lower() not in existing
    ][:_SUGGESTION_LIMIT]

    return templates.TemplateResponse(
        request,
        "app_icons_admin.html",
        {
            "title": "App icons",
            "active_nav": "settings",
            "items": items,
            "suggested": suggested,
        },
    )


__all__ = ["router"]
