"""List + view auto-generated monthly LLM digests (v0.68)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(prefix="/digest", tags=["digest"])


def _is_valid_month(month_iso: str) -> bool:
    """Return True iff ``month_iso`` is a well-formed ``YYYY-MM`` string."""
    parts = month_iso.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        return False
    if not (parts[0].isdigit() and parts[1].isdigit()):
        return False
    month = int(parts[1])
    return 1 <= month <= 12


@router.get("/monthly-archive", response_class=HTMLResponse)
async def monthly_index(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT month, provider, generated_at, length(body) AS len "
            "FROM monthly_digest ORDER BY month DESC LIMIT 60"
        )
        rows = await cursor.fetchall()
    digests = [
        {
            "month": str(row["month"]),
            "provider": row["provider"],
            "generated_at": str(row["generated_at"]),
            "length": int(row["len"]),
        }
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "monthly_digests.html",
        {
            "title": "Monthly digests",
            "active_nav": "digest",
            "digests": digests,
        },
    )


@router.get("/monthly-archive/{month}", response_class=HTMLResponse)
async def monthly_detail(request: Request, month: str) -> HTMLResponse:
    if not _is_valid_month(month):
        raise HTTPException(status_code=404, detail="Invalid month format")

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT month, body, provider, generated_at "
            "FROM monthly_digest WHERE month = ?",
            (month,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No digest for that month")
    month_iso = str(row["month"])
    return templates.TemplateResponse(
        request,
        "monthly_digest_detail.html",
        {
            "title": f"Monthly digest · {month_iso}",
            "active_nav": "digest",
            "month": month_iso,
            "body": str(row["body"]),
            "provider": row["provider"],
            "generated_at": str(row["generated_at"]),
        },
    )
