"""Search route — FTS5 keyword + optional semantic ranking."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.embeddings import EmbeddingsNotAvailable, is_available, semantic_search
from app.embeddings.model import load_model  # noqa: F401 (eager-import sanity)
from app.search import search as run_search
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.search_history import clear_history, list_recent, record_query
from app.web.templates_engine import templates

router = APIRouter(tags=["search"])


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query(default=""),
    app_name: str | None = Query(default=None, alias="app"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    mode: str = Query(default="hybrid"),
    tier: str | None = Query(default=None),
    tag: str | None = Query(default=None),
) -> HTMLResponse:
    """Render search page with optional tier / tag post-filters."""
    since_dt = _parse_iso_or_none(since)
    until_dt = _parse_iso_or_none(until)

    settings = get_settings()
    chosen_mode = _coerce_mode(mode, settings.embeddings_enabled and is_available())

    hits: list[Any] = []
    semantic_unavailable: str | None = None
    recent_searches: list[dict[str, Any]] = []

    async with get_connection() as conn:
        if q:
            if chosen_mode in {"hybrid", "fts"}:
                fts_hits = await run_search(
                    conn,
                    query=q,
                    limit=100,
                    since=since_dt,
                    until=until_dt,
                    app_name=app_name,
                )
            else:
                fts_hits = []

            sem_hits: list[Any] = []
            if chosen_mode in {"hybrid", "semantic"}:
                try:
                    sem_hits = await semantic_search(
                        conn,
                        query=q,
                        limit=100,
                        since=since_dt,
                        until=until_dt,
                        app_name=app_name,
                    )
                except EmbeddingsNotAvailable as exc:
                    semantic_unavailable = str(exc)
                    sem_hits = []

            await record_query(conn, query=q, mode=chosen_mode)
            hits = _merge_results(fts_hits, sem_hits, mode=chosen_mode)

        recent_searches = await list_recent(conn)

    if q and (tier or tag):
        hits = await _apply_post_filters(hits, tier=tier, tag=tag)

    is_htmx = request.headers.get("HX-Request") == "true"
    template_name = "_search_results.html" if is_htmx else "search.html"

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "title": f"Search: {q}" if q else "Search",
            "active_nav": "search",
            "query": q,
            "app_name": app_name,
            "since": since,
            "until": until,
            "mode": chosen_mode,
            "tier": tier,
            "tag": tag,
            "hits": hits,
            "total": len(hits),
            "embeddings_enabled": settings.embeddings_enabled,
            "embeddings_available": is_available(),
            "semantic_unavailable": semantic_unavailable,
            "recent_searches": recent_searches,
        },
    )


@router.get("/api/search-history")
async def api_search_history() -> JSONResponse:
    """Return the recent search history as JSON."""
    async with get_connection() as conn:
        recent = await list_recent(conn)
    return JSONResponse({"recent": recent})


@router.post("/api/search-history/clear")
async def api_search_history_clear() -> JSONResponse:
    """Wipe search history; return how many rows were deleted."""
    async with get_connection() as conn:
        deleted = await clear_history(conn)
    return JSONResponse({"deleted": deleted})


def _coerce_mode(value: str, semantic_ready: bool) -> str:
    mode = value.lower().strip()
    if mode not in {"fts", "semantic", "hybrid"}:
        mode = "hybrid"
    if mode in {"semantic", "hybrid"} and not semantic_ready:
        return "fts"
    return mode


def _merge_results(fts_hits: list[Any], sem_hits: list[Any], *, mode: str) -> list[dict[str, Any]]:
    """Merge keyword + semantic results into a single ranked list."""
    out: dict[int, dict[str, Any]] = {}

    for rank, h in enumerate(fts_hits, start=1):
        out[h.screenshot_id] = {
            "screenshot_id": h.screenshot_id,
            "captured_at": h.captured_at,
            "thumbnail_path": h.thumbnail_path,
            "app_name": h.app_name,
            "window_title": h.window_title,
            "snippet": h.snippet,
            "fts_rank": rank,
            "similarity": None,
            "score": 1.0 / rank,
        }

    for sem in sem_hits:
        sid = sem["screenshot_id"]
        if sid in out:
            out[sid]["similarity"] = sem["similarity"]
            out[sid]["score"] = out[sid]["score"] + sem["similarity"]
        else:
            out[sid] = {
                "screenshot_id": sid,
                "captured_at": sem["captured_at"],
                "thumbnail_path": sem["thumbnail_path"],
                "app_name": sem["app_name"],
                "window_title": sem["window_title"],
                "snippet": sem["snippet"],
                "fts_rank": None,
                "similarity": sem["similarity"],
                "score": sem["similarity"],
            }

    merged = sorted(out.values(), key=lambda h: h["score"], reverse=True)
    if mode == "semantic":
        merged = [h for h in merged if h["similarity"] is not None]
    return merged[:100]


async def _apply_post_filters(
    hits: list[dict[str, Any]],
    *,
    tier: str | None,
    tag: str | None,
) -> list[dict[str, Any]]:
    """Filter merged hits by tier / tag (resolved via DB lookups)."""
    if not hits:
        return hits
    ids = [int(h["screenshot_id"]) for h in hits]
    keep_ids: set[int] = set(ids)

    async with get_connection() as conn:
        if tier:
            placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                f"SELECT id FROM screenshots WHERE id IN ({placeholders}) AND tier = ?",
                (*ids, tier),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["id"]) for row in rows}

        if tag:
            placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                f"SELECT st.screenshot_id FROM screenshot_tags st "
                f"JOIN tags t ON t.id = st.tag_id "
                f"WHERE t.name = ? AND st.screenshot_id IN ({placeholders})",
                (tag, *ids),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["screenshot_id"]) for row in rows}

    return [h for h in hits if int(h["screenshot_id"]) in keep_ids]


def _parse_iso_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
