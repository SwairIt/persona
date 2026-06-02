"""HTTP API + UI for tagging screenshots and saved searches."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.search import search as run_fts_search
from app.storage.tags import (
    co_tag_counts,
    create_tag,
    delete_saved_search,
    delete_tag,
    get_screenshot_tags,
    list_saved_searches,
    list_screenshots_by_tag,
    list_tags,
    merge_tag,
    per_day_for_tag,
    rename_tag,
    save_search,
    saved_search_mark_seen,
    saved_search_new_count,
    set_tag_color,
    tag_screenshot,
    untag_screenshot,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["tags"])


@router.get("/tags", response_class=HTMLResponse)
async def tags_page(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        tags = await list_tags(conn)
        saved = await list_saved_searches(conn)
        for s in saved:
            try:
                s["new_count"] = await saved_search_new_count(
                    conn, search_id=s["id"], fts_query_callback=run_fts_search,
                )
            except Exception:
                s["new_count"] = 0
    return templates.TemplateResponse(
        request,
        "tags.html",
        {
            "title": "Tags & saved",
            "active_nav": "tags",
            "tags": tags,
            "saved_searches": saved,
        },
    )


@router.post("/api/tags/{tag_id}/color", response_class=JSONResponse)
async def set_color_api(tag_id: int, color: str = Form(default="")) -> JSONResponse:
    async with get_connection() as conn:
        try:
            await set_tag_color(conn, tag_id, color=color or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"id": tag_id, "color": color or None})


@router.post("/api/saved-searches/{search_id}/mark-seen", response_class=JSONResponse)
async def mark_seen_api(search_id: int) -> JSONResponse:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT MAX(id) AS max_id FROM screenshots"
        )
        row = await cursor.fetchone()
        highest = int(row["max_id"] or 0) if row else 0
        await saved_search_mark_seen(conn, search_id=search_id, highest_id=highest)
    return JSONResponse({"search_id": search_id, "marked_seen_at": highest})


@router.get("/tags/{tag_id}", response_class=HTMLResponse)
async def tag_detail(request: Request, tag_id: int) -> HTMLResponse:
    async with get_connection() as conn:
        all_tags = await list_tags(conn)
        tag = next((t for t in all_tags if t["id"] == tag_id), None)
        if tag is None:
            raise HTTPException(status_code=404, detail="Tag not found")
        shot_ids = await list_screenshots_by_tag(conn, tag_id, limit=500)
        shots = []
        for sid in shot_ids:
            shot = await get_screenshot(conn, sid)
            if shot is not None:
                shots.append(shot)
        co_tags = await co_tag_counts(conn, tag_id)
        per_day = await per_day_for_tag(conn, tag_id)
    return templates.TemplateResponse(
        request,
        "tag_detail.html",
        {
            "title": f"Tag: {tag['name']}",
            "active_nav": "tags",
            "tag": tag,
            "shots": shots,
            "co_tags": co_tags,
            "per_day": per_day,
            "all_tags": [t for t in all_tags if t["id"] != tag_id],
        },
    )


@router.post("/api/tags/{tag_id}/rename", response_class=JSONResponse)
async def rename_tag_api(tag_id: int, name: str = Form(...)) -> JSONResponse:
    if not name.strip():
        raise HTTPException(status_code=400, detail="Empty name")
    async with get_connection() as conn:
        try:
            await rename_tag(conn, tag_id, new_name=name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"id": tag_id, "name": name.strip().lower()})


@router.post("/api/tags/{tag_id}/merge-into/{target_id}", response_class=JSONResponse)
async def merge_tag_api(tag_id: int, target_id: int) -> JSONResponse:
    if tag_id == target_id:
        raise HTTPException(status_code=400, detail="Source and target are the same tag")
    async with get_connection() as conn:
        moved = await merge_tag(conn, source_id=tag_id, target_id=target_id)
    return JSONResponse({"moved": moved, "deleted_tag": tag_id, "target_tag": target_id})


@router.post("/api/tags/{tag_id}/delete", response_class=JSONResponse)
async def delete_tag_api(tag_id: int) -> JSONResponse:
    async with get_connection() as conn:
        await delete_tag(conn, tag_id)
    return JSONResponse({"deleted": tag_id})


@router.post("/api/tags/bulk-apply", response_class=JSONResponse)
async def bulk_apply_tag_api(
    tag_name: str = Form(...),
    screenshot_ids: str = Form(...),
) -> JSONResponse:
    """Apply a tag (auto-created if needed) to a comma-separated list of screenshot ids."""
    name = tag_name.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Empty tag name")
    try:
        ids = [int(s) for s in screenshot_ids.split(",") if s.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid screenshot id") from exc
    if not ids:
        raise HTTPException(status_code=400, detail="No screenshots selected")

    async with get_connection() as conn:
        tag_id = await create_tag(conn, name=name)
        applied = 0
        for sid in ids[:500]:
            await tag_screenshot(conn, sid, tag_id)
            applied += 1
    return JSONResponse({"tag": name, "applied": applied, "tag_id": tag_id})


@router.post("/api/tags", response_class=JSONResponse)
async def create_tag_api(name: str = Form(...), color: str = Form(default="")) -> JSONResponse:
    if not name.strip():
        raise HTTPException(status_code=400, detail="Empty tag name")
    async with get_connection() as conn:
        tag_id = await create_tag(conn, name=name, color=color or None)
    return JSONResponse({"id": tag_id, "name": name.strip()})


@router.post("/api/screenshots/{screenshot_id}/tags", response_class=JSONResponse)
async def attach_tag(screenshot_id: int, tag_id: int = Form(...)) -> JSONResponse:
    async with get_connection() as conn:
        await tag_screenshot(conn, screenshot_id, tag_id)
        current = await get_screenshot_tags(conn, screenshot_id)
    return JSONResponse({"screenshot_id": screenshot_id, "tags": current})


@router.delete("/api/screenshots/{screenshot_id}/tags/{tag_id}", response_class=JSONResponse)
async def detach_tag(screenshot_id: int, tag_id: int) -> JSONResponse:
    async with get_connection() as conn:
        await untag_screenshot(conn, screenshot_id, tag_id)
        current = await get_screenshot_tags(conn, screenshot_id)
    return JSONResponse({"screenshot_id": screenshot_id, "tags": current})


@router.post("/api/saved-searches", response_class=JSONResponse)
async def save_search_api(
    name: str = Form(...),
    query: str = Form(...),
    app_name: str = Form(default=""),
) -> JSONResponse:
    if not name.strip():
        raise HTTPException(status_code=400, detail="Name required")
    async with get_connection() as conn:
        search_id = await save_search(conn, name=name, query=query, app_name=app_name or None)
    return JSONResponse({"id": search_id})


@router.delete("/api/saved-searches/{search_id}")
async def delete_saved_search_api(search_id: int) -> RedirectResponse:
    async with get_connection() as conn:
        await delete_saved_search(conn, search_id)
    return RedirectResponse(url="/tags", status_code=303)
