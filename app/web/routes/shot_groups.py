"""HTTP layer for explicit, hand-curated screenshot groups.

Membership for these groups is *not* derived — see
:mod:`app.shot_groups` for the rationale. The routes are deliberately
shaped after :mod:`app.web.routes.auto_collections` so a user moving
between the two pages reads the same idioms (slug validation, delete
form, grid template).

Endpoints
---------
* ``GET  /groups``                              — index (list + create form)
* ``POST /groups``                              — create a new group
* ``POST /groups/{slug}/delete``                — drop the group + members
* ``GET  /groups/{slug}``                       — detail page (members grid)
* ``POST /api/shot/{shot_id}/group/{slug}/add``    — toggle-in via JSON
* ``POST /api/shot/{shot_id}/group/{slug}/remove`` — toggle-out via JSON

The two JSON endpoints exist so the per-shot UI (button on the
screenshot detail page, wired up later) can flip membership without a
full page reload. They return ``{"ok": true, "changed": <bool>}`` so
the caller can distinguish "we actually inserted/deleted" from
"membership was already in the requested state".
"""

from __future__ import annotations

import re

import aiosqlite
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.shot_groups import (
    add_member,
    create_group,
    delete_group,
    get_group,
    list_groups,
    members_of,
    remove_member,
)
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["shot-groups"])
log = get_logger("persona.shot_groups")

# Slug pattern mirrors auto_collections / facet_sets so all three "named
# bundle" features look the same in URLs and in logs.
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_TITLE_MIN, _TITLE_MAX = 1, 120

# Ceiling on how many members the detail page hydrates. Matches
# ``_MAX_SHOTS_PER_COLLECTION`` in :mod:`auto_collections` so the two
# grid pages have a consistent memory footprint and a consistent
# truncation story.
_MEMBER_RENDER_LIMIT = 500


def _validate_slug(slug: str) -> str:
    """Return the canonical slug or raise ``HTTPException(400)``."""
    cleaned = (slug or "").strip().lower()
    if not _SLUG_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Slug must match ^[a-z0-9-]{1,40}$",
        )
    return cleaned


def _validate_title(title: str) -> str:
    """Return the trimmed title or raise ``HTTPException(400)``."""
    cleaned = (title or "").strip()
    if not (_TITLE_MIN <= len(cleaned) <= _TITLE_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"Title must be {_TITLE_MIN}..{_TITLE_MAX} characters",
        )
    return cleaned


# ---------------------------------------------------------------------------
# HTML endpoints
# ---------------------------------------------------------------------------


@router.get("/groups", response_class=HTMLResponse)
async def groups_index(request: Request) -> HTMLResponse:
    """Render the index: every group + member count + add form."""
    groups = await list_groups()
    log.debug("shot_groups.list", count=len(groups))
    return templates.TemplateResponse(
        request,
        "shot_groups.html",
        {
            "title": "Screenshot groups",
            "active_nav": "tags",
            "groups": groups,
        },
    )


@router.post("/groups")
async def groups_create(
    slug: str = Form(...),
    title: str = Form(...),
) -> RedirectResponse:
    """Create a group and 303 back to the index (PRG)."""
    slug_v = _validate_slug(slug)
    title_v = _validate_title(title)
    try:
        await create_group(slug_v, title_v)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except aiosqlite.IntegrityError as exc:
        log.warning("shot_groups.duplicate", slug=slug_v)
        raise HTTPException(
            status_code=409,
            detail=f"Group {slug_v!r} already exists",
        ) from exc
    return RedirectResponse(url="/groups", status_code=303)


@router.post("/groups/{slug}/delete")
async def groups_delete(slug: str) -> RedirectResponse:
    """Drop the group and every membership row, then 303 to the index."""
    slug_v = _validate_slug(slug)
    removed = await delete_group(slug_v)
    if not removed:
        raise HTTPException(status_code=404, detail="Group not found")
    return RedirectResponse(url="/groups", status_code=303)


@router.get("/groups/{slug}", response_class=HTMLResponse)
async def groups_detail(request: Request, slug: str) -> HTMLResponse:
    """Render the members grid for one group.

    Missing shots (membership row points at a screenshot that has since
    been deleted) are silently skipped — they're a known race in the
    archive workflow and the page would otherwise 500 on a single stale
    member.
    """
    slug_v = _validate_slug(slug)
    group = await get_group(slug_v)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    members = await members_of(slug_v, limit=_MEMBER_RENDER_LIMIT)
    shots: list[object] = []
    async with get_connection() as conn:
        for member in members:
            shot = await get_screenshot(conn, member["shot_id"])
            if shot is not None:
                shots.append(shot)

    log.debug(
        "shot_groups.detail",
        slug=slug_v,
        members=len(members),
        hydrated=len(shots),
    )
    return templates.TemplateResponse(
        request,
        "shot_group_detail.html",
        {
            "title": group["title"],
            "active_nav": "tags",
            "group": group,
            "shots": shots,
            "shown": len(shots),
            "limit": _MEMBER_RENDER_LIMIT,
            "truncated": group["member_count"] > _MEMBER_RENDER_LIMIT,
        },
    )


# ---------------------------------------------------------------------------
# JSON toggle endpoints
# ---------------------------------------------------------------------------


@router.post("/api/shot/{shot_id}/group/{slug}/add")
async def api_shot_group_add(shot_id: int, slug: str) -> JSONResponse:
    """Toggle ``shot_id`` *into* ``slug``.

    Returns ``{"ok": true, "changed": <bool>}`` — ``changed`` is
    ``False`` when the shot was already a member.
    """
    if shot_id <= 0:
        raise HTTPException(status_code=400, detail="shot_id must be positive")
    slug_v = _validate_slug(slug)
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    try:
        changed = await add_member(slug_v, shot_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "changed": changed})


@router.post("/api/shot/{shot_id}/group/{slug}/remove")
async def api_shot_group_remove(shot_id: int, slug: str) -> JSONResponse:
    """Toggle ``shot_id`` *out of* ``slug``.

    Returns ``{"ok": true, "changed": <bool>}`` — ``changed`` is
    ``False`` when the shot was not a member to begin with. We do not
    404 in that case: from the caller's perspective the post-condition
    ("shot is not in the group") is satisfied either way.
    """
    if shot_id <= 0:
        raise HTTPException(status_code=400, detail="shot_id must be positive")
    slug_v = _validate_slug(slug)
    try:
        changed = await remove_member(slug_v, shot_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "changed": changed})
