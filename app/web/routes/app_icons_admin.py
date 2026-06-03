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

Bulk refresh (v0.82)
--------------------
``POST /settings/app-icons/refresh-all`` deletes every cached row whose
``source != 'user'`` so the auto-generated tiles regenerate lazily on
the next ``GET /app-icon/{name}.png``. Operator-uploaded ``user`` rows
are left untouched — those are not a function of the initials algorithm
and re-running the generator would silently overwrite hand-picked PNGs.
Every invocation lands in :mod:`app.audit` so a security review can
trace bulk invalidations back to the actor that triggered them.
"""

from __future__ import annotations

from typing import Final

import aiosqlite
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.app_icons import list_known_icons
from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.app_icons.admin")
refresh_log = get_logger("persona.app_icons.refresh")

router = APIRouter(tags=["app-icons"])

# SQL slug for the bulk refresh delete. Keeping the predicate as a
# module-level constant means the audit log + tests + this handler all
# reference the same "auto rows only, never user uploads" definition.
_REFRESH_DELETE_SQL: Final[str] = "DELETE FROM app_icon WHERE source != 'user'"

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


def _wants_html(request: Request) -> bool:
    """Return True when the caller is a browser form, not a JSON client.

    Mirrors the heuristic in :mod:`app.web.routes.app_icons` so the
    admin POST forms get a post-redirect-get loop while a future JSON
    caller (``Accept: application/json``) receives a 204 No Content
    without a wasted redirect round-trip.
    """
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


@router.post("/settings/app-icons/refresh-all")
async def app_icons_refresh_all(request: Request) -> Response:
    """Invalidate every auto-generated icon row in one transaction.

    Drops every ``app_icon`` row whose ``source != 'user'`` — so
    ``shell32`` and ``initials`` rows go away and regenerate lazily on
    the next ``GET /app-icon/{name}.png``, while operator-uploaded
    ``user`` overrides stay intact. The total deletion is wrapped in a
    single transaction so a concurrent ``get_icon_png`` write cannot
    interleave a half-cleared cache.

    Failure modes are bounded: a SQLite error is structured-logged and
    bubbles a 500, but the audit row is recorded with ``success=False``
    *before* the exception propagates so an operator can still trace
    the attempt. The handler is otherwise idempotent — calling it twice
    in a row simply records two audit rows and deletes zero on the
    second call.
    """
    deleted = 0
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(_REFRESH_DELETE_SQL)
            deleted = int(cursor.rowcount or 0)
            await conn.commit()
    except aiosqlite.Error as exc:
        refresh_log.exception("app_icons.refresh.failed", error=str(exc))
        await log_action(
            "app_icons.refresh_all",
            target="app_icon",
            detail=f"error={exc}",
            success=False,
        )
        raise

    refresh_log.info("app_icons.refresh.ok", deleted=deleted)
    await log_action(
        "app_icons.refresh_all",
        target="app_icon",
        detail=f"deleted={deleted} (source!=user)",
    )

    if _wants_html(request):
        return RedirectResponse(url="/settings/app-icons", status_code=303)
    return Response(status_code=204)


__all__ = ["router"]
