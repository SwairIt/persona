"""Admin UI for per-app capture-interval overrides."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.storage.app_overrides import (
    delete_override,
    list_overrides,
    upsert_override,
)
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["app-overrides"])


@router.get("/app-overrides", response_class=HTMLResponse)
async def overrides_page(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        items = await list_overrides(conn)
        existing = {item["app_name"] for item in items}
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC LIMIT 64"
        )
        rows = await cursor.fetchall()
        suggested = [
            {"app_name": str(row["app_name"]), "count": int(row["n"])}
            for row in rows
            if str(row["app_name"]) not in existing
        ][:12]
    return templates.TemplateResponse(
        request,
        "app_overrides.html",
        {
            "title": "Per-app capture interval",
            "active_nav": "settings",
            "items": items,
            "suggested": suggested,
        },
    )


@router.post("/app-overrides")
async def overrides_create(
    app_name: str = Form(...),
    interval_seconds: float = Form(...),
) -> RedirectResponse:
    async with get_connection() as conn:
        try:
            await upsert_override(
                conn,
                app_name=app_name,
                interval_seconds=interval_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/app-overrides", status_code=303)


@router.post("/app-overrides/{app_name}/delete")
async def overrides_delete(app_name: str) -> RedirectResponse:
    async with get_connection() as conn:
        await delete_override(conn, app_name)
    return RedirectResponse(url="/app-overrides", status_code=303)
