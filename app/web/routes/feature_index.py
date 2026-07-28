"""HTTP entry points for the full-app feature index.

Two endpoints:

* ``GET /feature-index`` — owner-facing page listing every browsable route with
  a live filter box.
* ``GET /api/features.json`` — same data as JSON for tooling and the
  Cmd+K command palette.
"""

from __future__ import annotations

from itertools import groupby
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.feature_index import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    FeatureEntry,
    build_feature_index,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from fastapi import FastAPI

log = get_logger("persona.feature_index")

router = APIRouter(tags=["features"])


def _group_by_category(entries: list[FeatureEntry]) -> list[tuple[str, str, list[FeatureEntry]]]:
    """Group entries into ``(category_slug, human_label, rows)`` tuples.

    ``build_feature_index`` already sorts by category index, so we can
    rely on :func:`itertools.groupby` here without an extra sort pass.
    """
    order_index: dict[str, int] = {str(cat): i for i, cat in enumerate(CATEGORY_ORDER)}
    fallback = len(order_index)
    grouped: list[tuple[str, str, list[FeatureEntry]]] = []
    for cat, rows in groupby(entries, key=lambda e: e["category"]):
        label = CATEGORY_LABELS.get(cat, str(cat).title())
        grouped.append((str(cat), label, list(rows)))
    grouped.sort(key=lambda t: order_index.get(t[0], fallback))
    return grouped


@router.get("/feature-index", response_class=HTMLResponse)
async def features_page(request: Request) -> HTMLResponse:
    """Render the discovery page with cards grouped by category."""
    app: FastAPI = request.app
    entries = await build_feature_index(app)
    grouped = _group_by_category(entries)

    log.info("feature_index.page_render", entries=len(entries), groups=len(grouped))

    return templates.TemplateResponse(
        request,
        "feature_index.html",
        {
            "title": "Features",
            "active_nav": "settings",
            "entries": entries,
            "grouped": grouped,
            "total": len(entries),
        },
    )


@router.get("/api/features.json", response_class=JSONResponse)
async def features_json(request: Request) -> JSONResponse:
    """Return the feature index as JSON for tooling."""
    app: FastAPI = request.app
    entries = await build_feature_index(app)
    log.info("feature_index.json_served", entries=len(entries))
    return JSONResponse({"total": len(entries), "entries": entries})
