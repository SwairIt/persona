"""List + view auto-generated weekly LLM digests."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(prefix="/digest", tags=["digest"])


def _week_end(week_start_iso: str) -> str:
    try:
        start = date.fromisoformat(week_start_iso)
    except ValueError:
        return ""
    return (start + timedelta(days=6)).isoformat()


@router.get("/weekly-archive", response_class=HTMLResponse)
async def weekly_index(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT week_start, provider, generated_at, length(body) AS len "
            "FROM weekly_digest ORDER BY week_start DESC LIMIT 60"
        )
        rows = await cursor.fetchall()
    digests = [
        {
            "week_start": str(row["week_start"]),
            "week_end": _week_end(str(row["week_start"])),
            "provider": row["provider"],
            "generated_at": str(row["generated_at"]),
            "length": int(row["len"]),
        }
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "weekly_digests.html",
        {
            "title": "Weekly digests",
            "active_nav": "digest",
            "digests": digests,
        },
    )


@router.get("/weekly-archive/{week_start}", response_class=HTMLResponse)
async def weekly_detail(request: Request, week_start: str) -> HTMLResponse:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT week_start, body, provider, generated_at "
            "FROM weekly_digest WHERE week_start = ?",
            (week_start,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No digest for that week")
    week_start_iso = str(row["week_start"])
    return templates.TemplateResponse(
        request,
        "weekly_digest_detail.html",
        {
            "title": f"Weekly digest · {week_start_iso}",
            "active_nav": "digest",
            "week_start": week_start_iso,
            "week_end": _week_end(week_start_iso),
            "body": str(row["body"]),
            "provider": row["provider"],
            "generated_at": str(row["generated_at"]),
        },
    )
