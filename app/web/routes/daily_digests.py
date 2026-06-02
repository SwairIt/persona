"""List + view auto-generated daily digests."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(prefix="/digest", tags=["digest"])


@router.get("/daily", response_class=HTMLResponse)
async def daily_index(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT day, provider, generated_at, length(body) AS len "
            "FROM daily_digest ORDER BY day DESC LIMIT 60"
        )
        rows = await cursor.fetchall()
    digests = [
        {
            "day": str(row["day"]),
            "provider": row["provider"],
            "generated_at": str(row["generated_at"]),
            "length": int(row["len"]),
        }
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "daily_digests.html",
        {"title": "Daily digests", "active_nav": "digest", "digests": digests},
    )


@router.get("/daily/{day}", response_class=HTMLResponse)
async def daily_detail(request: Request, day: str) -> HTMLResponse:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT day, body, provider, generated_at FROM daily_digest WHERE day = ?",
            (day,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No digest for that day")
    return templates.TemplateResponse(
        request,
        "daily_digest_detail.html",
        {
            "title": f"Digest · {day}",
            "active_nav": "digest",
            "day": str(row["day"]),
            "body": str(row["body"]),
            "provider": row["provider"],
            "generated_at": str(row["generated_at"]),
        },
    )
