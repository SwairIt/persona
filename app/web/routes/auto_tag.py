"""LLM-suggested tags endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.llm import LLMNotConfigured, suggest_tags
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.tags import create_tag, tag_screenshot
from app.tag_aliases import resolve as resolve_tag_alias

router = APIRouter(tags=["auto-tag"])


@router.post("/api/screenshots/{screenshot_id}/auto-tag-suggest", response_class=JSONResponse)
async def auto_tag_suggest(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    if shot.is_private:
        raise HTTPException(status_code=400, detail="Cannot suggest tags for a private screenshot")
    try:
        tags = await suggest_tags(
            app_name=shot.app_name,
            window_title=shot.window_title,
            ocr_text=shot.ocr_text,
        )
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"screenshot_id": screenshot_id, "suggested": tags})


@router.post("/api/screenshots/{screenshot_id}/auto-tag-apply", response_class=JSONResponse)
async def auto_tag_apply(
    screenshot_id: int,
    tags: str = Form(default=""),
) -> JSONResponse:
    """Apply a comma-separated list of tags. Creates tags as needed."""
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
    if not tag_list:
        raise HTTPException(status_code=400, detail="No tags supplied")
    # Route every candidate name through the alias overlay before it
    # touches the tag store, so two spellings of the same concept
    # ("standup" / "daily-standup") collapse onto one canonical row
    # instead of accreting as separate facets.
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in tag_list[:10]:
        canonical = await resolve_tag_alias(raw)
        if canonical and canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    applied: list[str] = []
    async with get_connection() as conn:
        if (await get_screenshot(conn, screenshot_id)) is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        for name in resolved:
            tag_id = await create_tag(conn, name=name)
            await tag_screenshot(conn, screenshot_id, tag_id)
            applied.append(name)
    return JSONResponse({"screenshot_id": screenshot_id, "applied": applied})
