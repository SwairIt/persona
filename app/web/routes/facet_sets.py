"""Saved facet sets — bundle a whole /search filter combination under a slug.

Where :mod:`app.web.routes.saved_searches` pins just the FTS ``q`` string,
a *facet set* captures every post-filter the search route accepts —
``app``, ``date_from`` / ``date_to``, ``since`` / ``until``, one or more
``tag`` values, ``tier``, ``min_w`` / ``min_h``, ``mode``, ``sort_by`` —
so re-running the saved slug reproduces the exact result set the operator
saw at save time.

The params are stored as a flat JSON blob in the ``facet_set.params_json``
column (see migration 078). All SQL uses bind parameters; the params
payload is parsed back through :func:`json.loads` and re-serialised into
a query string at recall time so user input never reaches the URL builder
unsanitised.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

import aiosqlite
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.facet_sets")

router = APIRouter(tags=["facet-sets"])

# Slug shape — narrow on purpose so the slug can be dropped into a URL
# path segment without percent-encoding and stays readable in logs.
SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
TITLE_MIN, TITLE_MAX = 1, 120

# Hard cap on the serialised params blob. The /search route only has a
# handful of single-string params plus a repeatable ``tag``, so a healthy
# payload is well under 1 KiB — anything bigger almost certainly means a
# caller is trying to stuff the wrong thing into the column.
PARAMS_JSON_MAX = 4096

# Whitelist of params we forward back to /search. Anything outside this
# set is silently dropped at save time so a malformed POST cannot smuggle
# unrelated query keys (or, worse, ``hx-*``-style hints) into the stored
# blob and then back into the redirect URL on recall.
_SCALAR_PARAMS: frozenset[str] = frozenset(
    {
        "q",
        "app",
        "since",
        "until",
        "date_from",
        "date_to",
        "mode",
        "tier",
        "min_w",
        "min_h",
        "sort_by",
    },
)
_LIST_PARAMS: frozenset[str] = frozenset({"tag"})


def _validate_slug(slug: str) -> str:
    cleaned = (slug or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        msg = "slug must match ^[a-z0-9-]{1,40}$"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_title(title: str) -> str:
    cleaned = (title or "").strip()
    if not (TITLE_MIN <= len(cleaned) <= TITLE_MAX):
        msg = f"title must be {TITLE_MIN}..{TITLE_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _parse_params_json(raw: str) -> dict[str, Any]:
    """Parse the incoming params blob into the storable subset.

    Accepts a JSON object whose values are strings or lists of strings.
    Anything outside :data:`_SCALAR_PARAMS` / :data:`_LIST_PARAMS` is
    dropped. Empty strings and empty lists are filtered out so a freshly
    opened /search page with no filters round-trips as ``"{}"`` rather
    than echoing every blank input back into the URL.
    """
    if len(raw) > PARAMS_JSON_MAX:
        msg = f"params_json must be <= {PARAMS_JSON_MAX} bytes"
        raise HTTPException(status_code=400, detail=msg)
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="params_json is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=400, detail="params_json must decode to an object")

    cleaned: dict[str, Any] = {}
    for key, value in decoded.items():
        if not isinstance(key, str):
            continue
        if key in _SCALAR_PARAMS:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                cleaned[key] = text
        elif key in _LIST_PARAMS:
            if not isinstance(value, list):
                continue
            items = [str(v).strip() for v in value if str(v).strip()]
            if items:
                cleaned[key] = items
        # Silently drop unknown keys — see module docstring.
    return cleaned


def _flatten_for_query(params: dict[str, Any]) -> list[tuple[str, str]]:
    """Convert the stored params dict into ordered ``(key, value)`` pairs.

    Multi-valued keys expand to one pair per entry so :func:`urlencode`
    can emit them as repeated query params (matching what the /search
    route reads via ``Query(default=None)`` on a ``list[str]``).
    """
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, list):
            for entry in value:
                pairs.append((key, str(entry)))
        else:
            pairs.append((key, str(value)))
    return pairs


async def _list_all(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT slug, title, params_json, created_at FROM facet_set ORDER BY created_at DESC",
    )
    rows = await cursor.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            params = json.loads(str(row["params_json"]))
        except json.JSONDecodeError:
            # A corrupt blob shouldn't take the whole page down — show
            # the slug so the operator can delete it, but render empty
            # params so the template's loop is safe.
            log.warning("facet_sets.row.corrupt_json", slug=str(row["slug"]))
            params = {}
        items.append(
            {
                "slug": str(row["slug"]),
                "title": str(row["title"]),
                "params": params,
                "created_at": str(row["created_at"]),
            },
        )
    return items


async def _get_one(
    conn: aiosqlite.Connection,
    slug: str,
) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "SELECT slug, title, params_json, created_at FROM facet_set WHERE slug = ?",
        (slug,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        params = json.loads(str(row["params_json"]))
    except json.JSONDecodeError as exc:
        log.warning("facet_sets.get.corrupt_json", slug=slug)
        msg = "stored params payload is corrupt"
        raise HTTPException(status_code=500, detail=msg) from exc
    return {
        "slug": str(row["slug"]),
        "title": str(row["title"]),
        "params": params,
        "created_at": str(row["created_at"]),
    }


@router.get("/searches/facets", response_class=HTMLResponse)
async def facet_sets_page(request: Request) -> HTMLResponse:
    """Render the saved facet sets index plus the manual create form."""
    async with get_connection() as conn:
        items = await _list_all(conn)
    log.debug("facet_sets.list", count=len(items))
    return templates.TemplateResponse(
        request,
        "facet_sets.html",
        {
            "title": "Saved facet sets",
            "active_nav": "search",
            "items": items,
        },
    )


@router.post("/searches/facets")
async def facet_sets_create(
    slug: str = Form(...),
    title: str = Form(...),
    params_json: str = Form(...),
) -> RedirectResponse:
    """Persist a new facet set under ``slug`` with the given params blob."""
    slug_v = _validate_slug(slug)
    title_v = _validate_title(title)
    cleaned_params = _parse_params_json(params_json)
    # Re-serialise from the cleaned dict so the stored payload only ever
    # contains the whitelisted subset, even if the client sent extras.
    payload = json.dumps(cleaned_params, ensure_ascii=False, sort_keys=True)

    async with get_connection() as conn:
        try:
            await conn.execute(
                "INSERT INTO facet_set (slug, title, params_json) VALUES (?, ?, ?)",
                (slug_v, title_v, payload),
            )
            await conn.commit()
        except aiosqlite.IntegrityError as exc:
            log.warning("facet_sets.duplicate", slug=slug_v)
            msg = f"slug {slug_v!r} already exists"
            raise HTTPException(status_code=400, detail=msg) from exc

    log.info(
        "facet_sets.created",
        slug=slug_v,
        title=title_v,
        param_keys=sorted(cleaned_params.keys()),
    )
    return RedirectResponse(url="/searches/facets", status_code=303)


@router.post("/searches/facets/{slug}/delete")
async def facet_sets_delete(slug: str) -> RedirectResponse:
    """Drop a facet set by slug (no-op when the row is already gone)."""
    slug_v = _validate_slug(slug)
    async with get_connection() as conn:
        await conn.execute("DELETE FROM facet_set WHERE slug = ?", (slug_v,))
        await conn.commit()
    log.info("facet_sets.deleted", slug=slug_v)
    return RedirectResponse(url="/searches/facets", status_code=303)


@router.get("/searches/facets/{slug}")
async def facet_sets_run(slug: str) -> RedirectResponse:
    """Recall a facet set: 303 redirect to /search with the flattened params."""
    slug_v = _validate_slug(slug)
    async with get_connection() as conn:
        record = await _get_one(conn, slug_v)
    if record is None:
        raise HTTPException(status_code=404, detail="Facet set not found")
    pairs = _flatten_for_query(record["params"])
    query_string = urlencode(pairs, doseq=False)
    target = "/search" if not query_string else f"/search?{query_string}"
    log.info(
        "facet_sets.run",
        slug=slug_v,
        param_count=len(pairs),
    )
    return RedirectResponse(url=target, status_code=303)
