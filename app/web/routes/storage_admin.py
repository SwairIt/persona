"""User-facing /storage dashboard — disk usage + cleanup."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage_management import (
    get_settings,
    list_cleanup_runs,
    run_cleanup,
    set_settings,
    usage_breakdown,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["storage"])
log = get_logger("persona.storage_admin")


@router.get("/storage", response_class=HTMLResponse, response_model=None)
async def storage_dashboard(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    settings = await get_settings()
    usage = await usage_breakdown(recent_days=14)
    runs = await list_cleanup_runs(limit=10)
    return templates.TemplateResponse(
        request,
        "storage_dashboard.html",
        {
            "title": "Хранилище",
            "active_nav": "",
            "settings": settings,
            "usage": usage,
            "runs": runs,
        },
    )


@router.post("/storage/settings", response_model=None)
async def save_storage_settings(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    retention_days: Annotated[str, Form()] = "",
    quota_mb: Annotated[str, Form()] = "",
) -> RedirectResponse:
    r_int: int | None = None
    q_int: int | None = None
    if retention_days.strip():
        try:
            r_int = int(retention_days.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="retention_days must be a number") from exc
    if quota_mb.strip():
        try:
            q_int = int(quota_mb.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="quota_mb must be a number") from exc
    await set_settings(retention_days=r_int, quota_mb=q_int)
    return RedirectResponse(url="/storage", status_code=303)


@router.post("/storage/cleanup", response_model=None)
async def trigger_cleanup(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    older_than_days: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Run one cleanup pass. ``older_than_days`` lets the user override
    the saved retention for a one-shot purge."""
    override: int | None = None
    if older_than_days.strip():
        try:
            override = int(older_than_days.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="older_than_days must be a number"
            ) from exc
    await run_cleanup(trigger_source="manual", override_retention_days=override)
    return RedirectResponse(url="/storage", status_code=303)


@router.get("/api/storage/usage.json", response_class=JSONResponse)
async def storage_usage_json(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    usage = await usage_breakdown(recent_days=14)
    return JSONResponse(
        {
            "total_shots": usage.total_shots,
            "total_bytes": usage.total_bytes,
            "oldest_captured_at": usage.oldest_captured_at,
            "newest_captured_at": usage.newest_captured_at,
            "by_day_recent": usage.by_day_recent,
        }
    )
