"""HTMX-driven admin UI for :func:`app.dup_finder.find_suspected_duplicates`.

Three endpoints:

* ``GET  /admin/dup-finder`` — render the scanner page (form + empty
  results pane).
* ``POST /api/dup-finder/scan`` — run a scan with the given threshold
  and lookback, return the result groups as the result fragment.
* ``POST /api/dup-finder/delete-group`` — accept a JSON
  ``{"keep_id": int, "delete_ids": list[int]}`` body and soft-delete
  every id in ``delete_ids`` except ``keep_id``. Returns
  ``{"deleted": int}`` so the UI can update its counter without a
  full re-scan.

This module is intentionally NOT wired into ``app/web/main.py`` —
``dup_finder`` is a one-off cleanup tool, not a permanent surface.
The operator mounts the router from a one-shot startup hook or
imports it manually when they need a deep clean.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.audit import log_action
from app.dup_finder import find_suspected_duplicates, soft_delete_shots
from app.logging_setup import get_logger
from app.web.routes.thumbnails import thumbnail_url
from app.web.templates_engine import templates

log = get_logger("persona.web.dup_finder")

router = APIRouter(tags=["dup-finder"])

# Defaults mirror :func:`find_suspected_duplicates`. Echoed into the
# template so the form re-renders with the last-used values.
_DEFAULT_THRESHOLD: int = 6
_DEFAULT_LOOKBACK_DAYS: int = 30
_DEFAULT_LIMIT: int = 100

# Hard caps. The scan is async-friendly but a malicious / fat-fingered
# threshold of 64 on a 100k-row database would still chew CPU; clamp
# the user input rather than trust it.
_THRESHOLD_MAX: int = 32
_LOOKBACK_MAX_DAYS: int = 3650  # ten years; effectively "all history"
_GROUP_LIMIT_MAX: int = 500
_DELETE_BATCH_MAX: int = 5000


class _DeleteGroupBody(BaseModel):
    """JSON body for ``POST /api/dup-finder/delete-group``."""

    keep_id: int = Field(..., ge=1, description="Row id to keep — never deleted.")
    delete_ids: list[int] = Field(
        ...,
        min_length=1,
        max_length=_DELETE_BATCH_MAX,
        description="Row ids to soft-delete (``keep_id`` is filtered out).",
    )


def _validate_threshold(threshold: int) -> int:
    if threshold < 0 or threshold > _THRESHOLD_MAX:
        msg = f"threshold must be 0..{_THRESHOLD_MAX}"
        raise HTTPException(status_code=400, detail=msg)
    return threshold


def _validate_lookback(lookback_days: int) -> int:
    if lookback_days < 1 or lookback_days > _LOOKBACK_MAX_DAYS:
        msg = f"lookback_days must be 1..{_LOOKBACK_MAX_DAYS}"
        raise HTTPException(status_code=400, detail=msg)
    return lookback_days


def _validate_limit(limit: int) -> int:
    if limit < 1 or limit > _GROUP_LIMIT_MAX:
        msg = f"limit must be 1..{_GROUP_LIMIT_MAX}"
        raise HTTPException(status_code=400, detail=msg)
    return limit


@router.get("/admin/dup-finder", response_class=HTMLResponse)
async def dup_finder_page(request: Request) -> HTMLResponse:
    """Render the scanner page with the default form values."""
    return templates.TemplateResponse(
        request,
        "dup_finder.html",
        {
            "title": "Поиск дублей",
            "active_nav": "settings",
            "default_threshold": _DEFAULT_THRESHOLD,
            "default_lookback_days": _DEFAULT_LOOKBACK_DAYS,
            "default_limit": _DEFAULT_LIMIT,
            "result": None,
        },
    )


@router.post("/api/dup-finder/scan", response_class=HTMLResponse)
async def dup_finder_scan(
    request: Request,
    threshold: Annotated[int, Form()] = _DEFAULT_THRESHOLD,
    lookback_days: Annotated[int, Form()] = _DEFAULT_LOOKBACK_DAYS,
    limit: Annotated[int, Form()] = _DEFAULT_LIMIT,
) -> HTMLResponse:
    """Run the scan and re-render the full page with the result block."""
    threshold_v = _validate_threshold(threshold)
    lookback_v = _validate_lookback(lookback_days)
    limit_v = _validate_limit(limit)

    raw = await find_suspected_duplicates(
        threshold=threshold_v,
        limit=limit_v,
        lookback_days=lookback_v,
    )

    # Hydrate group dicts with per-shot thumbnail URLs so the template
    # can render the strip without poking the DB again.
    enriched_groups = await _hydrate_groups(raw["groups"])

    await log_action(
        "dup_finder.scan",
        target=f"threshold={threshold_v} lookback={lookback_v}d",
        detail=(
            f"scanned={raw['scanned']} "
            f"groups={len(enriched_groups)} "
            f"candidates={raw['candidates_total']}"
        ),
    )

    return templates.TemplateResponse(
        request,
        "dup_finder.html",
        {
            "title": "Поиск дублей",
            "active_nav": "settings",
            "default_threshold": threshold_v,
            "default_lookback_days": lookback_v,
            "default_limit": limit_v,
            "result": {
                "scanned": raw["scanned"],
                "candidates_total": raw["candidates_total"],
                "groups": enriched_groups,
            },
        },
    )


@router.post("/api/dup-finder/delete-group", response_class=JSONResponse)
async def dup_finder_delete_group(
    body: Annotated[_DeleteGroupBody, Body(...)],
) -> JSONResponse:
    """Soft-delete every id in ``body.delete_ids`` except ``body.keep_id``."""
    keep_id = body.keep_id
    delete_ids = list(dict.fromkeys(body.delete_ids))  # dedupe, preserve order

    deleted = await soft_delete_shots(keep_id=keep_id, delete_ids=delete_ids)

    await log_action(
        "dup_finder.delete_group",
        target=f"keep={keep_id}",
        detail=(
            f"requested={len(delete_ids)} "
            f"deleted={deleted}"
        ),
    )

    return JSONResponse({"deleted": deleted})


async def _hydrate_groups(
    groups: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Attach thumbnail URLs + per-shot metadata to each group dict.

    The pure-data result from :func:`find_suspected_duplicates` only
    has ids; the template needs thumbnails + ``captured_at`` for the
    strip. We do one extra SELECT per scan (not per group) to avoid
    the N+1 trap.
    """
    if not groups:
        return []

    # Collect every shot id across every group.
    all_ids: list[int] = []
    for group in groups:
        shot_ids = group["shot_ids"]
        if isinstance(shot_ids, list):
            all_ids.extend(int(i) for i in shot_ids)
    if not all_ids:
        return []

    # Parametrised IN (...) via a generated placeholder string. The
    # placeholders are ``?`` only — the values flow through the
    # ``execute(..., params)`` channel, never via string formatting.
    # ``placeholders`` is a string of ``?,?,?`` only — values flow
    # through the params tuple below, not the SQL string. Ruff's S608
    # cannot see that ``all_ids`` is bounded by the SELECT above and
    # never user-influenced beyond the row ids themselves.
    placeholders = ",".join("?" for _ in all_ids)
    sql = f"SELECT id, captured_at, app_name, thumbnail_path FROM screenshots WHERE id IN ({placeholders})"  # noqa: S608, E501

    from app.storage.db import get_connection  # noqa: PLC0415 — avoid cycle

    async with get_connection() as conn:
        cursor = await conn.execute(sql, tuple(all_ids))
        rows = await cursor.fetchall()

    by_id: dict[int, dict[str, object]] = {}
    for row in rows:
        raw_thumb = row["thumbnail_path"]
        thumb_url = thumbnail_url(raw_thumb) if raw_thumb is not None else None
        by_id[int(row["id"])] = {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "app_name": row["app_name"],
            "thumbnail_url": thumb_url,
        }

    hydrated: list[dict[str, object]] = []
    for group in groups:
        shot_ids = group["shot_ids"]
        if not isinstance(shot_ids, list):
            continue
        shots = [by_id[int(i)] for i in shot_ids if int(i) in by_id]
        hydrated.append(
            {
                "shot_ids": [int(i) for i in shot_ids],
                "count": group["count"],
                "first_captured_at": group["first_captured_at"],
                "suggested_keep_id": group["suggested_keep_id"],
                "shots": shots,
            },
        )
    return hydrated
