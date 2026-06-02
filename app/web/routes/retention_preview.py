"""Retention preview UI + JSON — v0.45.

``GET /admin/retention-preview`` renders a Tailwind page with three big
number cards (warm / cold / hard-delete) and a sample-thumbnail grid per
card, plus a banner reminding the user that nothing is being mutated.

``GET /api/retention-preview.json`` returns the same data as JSON for
scripts, dashboards or smoke tests.

Both endpoints delegate to :func:`app.retention_preview.preview` so the
SQL conditions stay in exactly one place — the moment the retention
worker's thresholds change, this page changes with it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.retention_preview import preview
from app.settings import get_settings
from app.storage.db import get_connection
from app.web.routes.thumbnails import thumbnail_url
from app.web.templates_engine import templates

router = APIRouter(tags=["retention-preview"])

log = get_logger("persona.retention.preview")


async def _hydrate_samples(ids: list[int], source: str) -> list[dict[str, Any]]:
    """Look up thumbnail paths for the sample IDs so the page can render them.

    ``source`` is either ``"screenshots"`` (for warm/cold buckets) or
    ``"recycle_bin"`` (for the hard-delete bucket — the row is no longer
    in ``screenshots`` once it has been soft-deleted). Returns rows in
    the same order as the input ``ids`` so the UI lines up with the
    counts.
    """
    if not ids:
        return []
    if source not in {"screenshots", "recycle_bin"}:
        msg = f"unknown sample source: {source}"
        raise ValueError(msg)

    placeholders = ",".join("?" for _ in ids)
    query = (
        f"SELECT id, thumbnail_path FROM {source} "  # noqa: S608 — table name is hard-coded above
        f"WHERE id IN ({placeholders})"
    )
    async with get_connection() as conn:
        cursor = await conn.execute(query, ids)
        rows = await cursor.fetchall()

    by_id: dict[int, str | None] = {}
    for row in rows:
        thumb = row["thumbnail_path"]
        by_id[int(row["id"])] = str(thumb) if thumb else None

    hydrated: list[dict[str, Any]] = []
    for sid in ids:
        path = by_id.get(sid)
        hydrated.append(
            {
                "id": sid,
                "thumbnail_url": thumbnail_url(path),
            }
        )
    return hydrated


@router.get("/admin/retention-preview", response_class=HTMLResponse)
async def retention_preview_page(request: Request) -> HTMLResponse:
    """Render the dry-run preview page."""
    data = await preview()
    settings = get_settings()

    warm_samples = await _hydrate_samples(data["sample_ids"]["warm"], "screenshots")
    cold_samples = await _hydrate_samples(data["sample_ids"]["cold"], "screenshots")
    delete_samples = await _hydrate_samples(data["sample_ids"]["delete"], "recycle_bin")

    bytes_freed = data["total_bytes_freed_estimate"]
    mb_freed = bytes_freed / (1024 * 1024) if bytes_freed else 0.0

    return templates.TemplateResponse(
        request,
        "retention_preview.html",
        {
            "title": "Retention preview",
            "active_nav": "settings",
            "preview": data,
            "warm_samples": warm_samples,
            "cold_samples": cold_samples,
            "delete_samples": delete_samples,
            "bytes_freed": bytes_freed,
            "mb_freed": mb_freed,
            "warm_after_days": settings.tier_warm_after_days,
            "cold_after_days": settings.tier_cold_after_days,
            "recycle_retention_days": settings.recycle_retention_days,
            "tiered_retention": settings.tiered_retention,
        },
    )


@router.get("/api/retention-preview.json")
async def retention_preview_json() -> JSONResponse:
    """Return the dry-run preview as JSON (same shape as :func:`preview`)."""
    data = await preview()
    return JSONResponse(content=dict(data))


__all__ = ["router"]
