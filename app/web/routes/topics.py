"""Topic discovery — auto-cluster recent captures into themes."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.embeddings import is_available
from app.embeddings.clustering import discover_clusters
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["topics"])


@router.get("/topics", response_class=HTMLResponse)
async def topics_page(
    request: Request,
    k: int = Query(default=8, ge=2, le=24),
) -> HTMLResponse:
    settings = get_settings()
    if not settings.embeddings_enabled or not is_available():
        return templates.TemplateResponse(
            request,
            "topics.html",
            {
                "title": "Topics",
                "active_nav": "topics",
                "ready": False,
                "k": k,
                "clusters": [],
                "samples": {},
            },
        )

    async with get_connection() as conn:
        clusters = await discover_clusters(conn, k=k)
        samples: dict[int, list] = {}
        for idx, cluster in enumerate(clusters):
            previews = []
            for sid in cluster.member_ids[:6]:
                shot = await get_screenshot(conn, sid)
                if shot is not None:
                    previews.append(shot)
            samples[idx] = previews

    return templates.TemplateResponse(
        request,
        "topics.html",
        {
            "title": "Topics",
            "active_nav": "topics",
            "ready": True,
            "k": k,
            "clusters": clusters,
            "samples": samples,
        },
    )
