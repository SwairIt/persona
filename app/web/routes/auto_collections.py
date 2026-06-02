"""Tag-driven auto-collections.

A rule binds a URL slug to a tag name. Visiting ``/collection/{slug}`` renders
every screenshot currently carrying that tag — the membership is computed on
read, so newly-tagged shots show up immediately with no maintenance.

Rules with ``public = 1`` are reachable from anywhere the FastAPI app is
exposed. Rules with ``public = 0`` are restricted to loopback (127.0.0.1 /
::1), which is the closest analogue to a session check in Persona's
local-first model (there is no user/session table to consult).
"""

from __future__ import annotations

import re
from ipaddress import ip_address
from typing import Any

import aiosqlite
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["collections"])
log = get_logger("persona.auto_collections")

# Slug constraint mirrors the spec: lowercase alphanumeric + hyphen, 1..40 chars.
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_MAX_SHOTS_PER_COLLECTION = 500


def _parse_public_flag(raw: str | None) -> int:
    """Accept the usual truthy strings from form posts; default to 0."""
    if raw is None:
        return 0
    return 1 if raw.strip().lower() in {"1", "true", "on", "yes"} else 0


def _validate_slug(slug: str) -> str:
    """Return the slug if it matches the canonical pattern, otherwise 400."""
    cleaned = slug.strip().lower()
    if not _SLUG_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Slug must match ^[a-z0-9-]{1,40}$",
        )
    return cleaned


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from the same host.

    Persona ships without an auth layer; loopback is the strongest signal we
    have that the caller is the local user. Used to gate private collections.
    """
    client = request.client
    if client is None:
        return False
    try:
        return ip_address(client.host).is_loopback
    except ValueError:
        return False


async def _fetch_rule(
    conn: aiosqlite.Connection, slug: str
) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "SELECT slug, title, tag, public, created_at FROM auto_collection WHERE slug = ?",
        (slug,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "slug": str(row["slug"]),
        "title": str(row["title"]),
        "tag": str(row["tag"]),
        "public": int(row["public"]),
        "created_at": str(row["created_at"]),
    }


async def _list_rules(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT slug, title, tag, public, created_at "
        "FROM auto_collection ORDER BY created_at DESC, slug ASC"
    )
    rows = await cursor.fetchall()
    return [
        {
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "tag": str(row["tag"]),
            "public": int(row["public"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def _shot_ids_for_tag(
    conn: aiosqlite.Connection, tag_name: str, *, limit: int
) -> list[int]:
    """Return screenshot ids carrying the given tag (case-insensitive)."""
    cursor = await conn.execute(
        "SELECT st.screenshot_id AS sid "
        "FROM screenshot_tags st "
        "JOIN tags t ON t.id = st.tag_id "
        "WHERE LOWER(t.name) = LOWER(?) "
        "ORDER BY st.screenshot_id DESC "
        "LIMIT ?",
        (tag_name, limit),
    )
    rows = await cursor.fetchall()
    return [int(row["sid"]) for row in rows]


@router.get("/collections", response_class=HTMLResponse)
async def collections_index(request: Request) -> HTMLResponse:
    """List every rule, with a form to add a new one."""
    async with get_connection() as conn:
        rules = await _list_rules(conn)
    return templates.TemplateResponse(
        request,
        "auto_collections.html",
        {
            "title": "Auto-collections",
            "active_nav": "tags",
            "rules": rules,
        },
    )


@router.post("/collections")
async def collections_create(
    slug: str = Form(...),
    title: str = Form(...),
    tag: str = Form(...),
    public: str | None = Form(default=None),
) -> RedirectResponse:
    """Create a new auto-collection rule."""
    clean_slug = _validate_slug(slug)
    clean_title = title.strip()
    clean_tag = tag.strip().lower()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Title required")
    if not clean_tag:
        raise HTTPException(status_code=400, detail="Tag required")
    public_flag = _parse_public_flag(public)

    async with get_connection() as conn:
        try:
            await conn.execute(
                "INSERT INTO auto_collection (slug, title, tag, public) "
                "VALUES (?, ?, ?, ?)",
                (clean_slug, clean_title, clean_tag, public_flag),
            )
            await conn.commit()
        except aiosqlite.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Slug {clean_slug!r} already exists",
            ) from exc

    log.info(
        "auto_collection_created",
        slug=clean_slug,
        tag=clean_tag,
        public=bool(public_flag),
    )
    return RedirectResponse(url="/collections", status_code=303)


@router.get("/collection/{slug}", response_class=HTMLResponse)
async def collection_view(request: Request, slug: str) -> HTMLResponse:
    """Render the screenshots currently carrying the rule's tag."""
    clean_slug = _validate_slug(slug)
    async with get_connection() as conn:
        rule = await _fetch_rule(conn, clean_slug)
        if rule is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        if rule["public"] == 0 and not _is_loopback_client(request):
            # No session store in Persona; loopback is our local-user proxy.
            raise HTTPException(
                status_code=403,
                detail="Private collection — local access only",
            )
        shot_ids = await _shot_ids_for_tag(
            conn, rule["tag"], limit=_MAX_SHOTS_PER_COLLECTION
        )
        shots: list[Any] = []
        for sid in shot_ids:
            shot = await get_screenshot(conn, sid)
            if shot is not None:
                shots.append(shot)

    return templates.TemplateResponse(
        request,
        "auto_collection.html",
        {
            "title": rule["title"],
            "active_nav": "tags",
            "rule": rule,
            "shots": shots,
            "count": len(shots),
        },
    )


@router.post("/collection/{slug}/delete")
async def collection_delete(slug: str) -> RedirectResponse:
    """Drop the rule. Screenshots and tags are untouched."""
    clean_slug = _validate_slug(slug)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM auto_collection WHERE slug = ?", (clean_slug,)
        )
        await conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Collection not found")
    log.info("auto_collection_deleted", slug=clean_slug)
    return RedirectResponse(url="/collections", status_code=303)


@router.get("/api/collections", response_class=JSONResponse)
async def collections_list_api() -> JSONResponse:
    """JSON twin of the index page — useful for the browser extension."""
    async with get_connection() as conn:
        rules = await _list_rules(conn)
    return JSONResponse({"rules": rules})
