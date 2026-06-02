"""Stripped-down mobile companion page — text-only, no thumbnails."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.storage.time import iso, parse_iso
from app.web.templates_engine import templates

router = APIRouter(tags=["mobile"])


@router.get("/m", response_class=HTMLResponse)
async def mobile_today(request: Request) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, window_title, ocr_text "
            "FROM screenshots WHERE captured_at >= ? "
            "ORDER BY captured_at DESC LIMIT 30",
            (iso(start),),
        )
        latest_rows = await cursor.fetchall()
        latest = [
            {
                "id": int(row["id"]),
                "captured_at": parse_iso(str(row["captured_at"])),
                "app_name": row["app_name"],
                "window_title": row["window_title"],
                "ocr_snippet": (row["ocr_text"] or "")[:160],
            }
            for row in latest_rows
        ]

        cursor = await conn.execute(
            "SELECT n.screenshot_id, n.body, n.updated_at, s.app_name "
            "FROM screenshot_notes n JOIN screenshots s ON s.id = n.screenshot_id "
            "ORDER BY n.updated_at DESC LIMIT 10"
        )
        note_rows = await cursor.fetchall()
        notes = [
            {
                "id": int(row["screenshot_id"]),
                "app_name": row["app_name"],
                "body": str(row["body"]),
                "updated_at": parse_iso(str(row["updated_at"])),
            }
            for row in note_rows
        ]

        cursor = await conn.execute(
            "SELECT day, body FROM daily_digest ORDER BY day DESC LIMIT 1"
        )
        digest_row = await cursor.fetchone()
        digest = (
            {"day": str(digest_row["day"]), "body": str(digest_row["body"])}
            if digest_row
            else None
        )

    return templates.TemplateResponse(
        request,
        "mobile.html",
        {
            "title": "Persona · mobile",
            "active_nav": "",
            "latest": latest,
            "notes": notes,
            "digest": digest,
        },
    )
