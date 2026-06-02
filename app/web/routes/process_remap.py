"""Admin UI for user-defined process-name → app-name remapping."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.process_remap import delete_remap, list_remaps, upsert_remap
from app.web.templates_engine import templates

router = APIRouter(tags=["process-remap"])


@router.get("/process-remap", response_class=HTMLResponse)
async def remap_page(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        items = await list_remaps(conn)
        cursor = await conn.execute(
            "SELECT DISTINCT process_name, COUNT(*) AS n FROM screenshots "
            "WHERE process_name IS NOT NULL "
            "GROUP BY process_name ORDER BY n DESC LIMIT 20"
        )
        rows = await cursor.fetchall()
        suggested = [
            {"process_name": str(row["process_name"]), "count": int(row["n"])}
            for row in rows
            if not any(item["process_name"] == str(row["process_name"]).lower() for item in items)
        ]
    return templates.TemplateResponse(
        request,
        "process_remap.html",
        {
            "title": "Process renames",
            "active_nav": "settings",
            "items": items,
            "suggested": suggested,
        },
    )


@router.post("/process-remap")
async def remap_create(
    process_name: str = Form(...),
    app_name: str = Form(...),
) -> RedirectResponse:
    async with get_connection() as conn:
        try:
            await upsert_remap(conn, process_name=process_name, app_name=app_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/process-remap", status_code=303)


@router.post("/process-remap/{process_name}/delete")
async def remap_delete(process_name: str) -> RedirectResponse:
    async with get_connection() as conn:
        await delete_remap(conn, process_name)
    return RedirectResponse(url="/process-remap", status_code=303)
