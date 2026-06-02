"""Journal view — only screenshots that have user notes, ordered by recency."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.storage.time import parse_iso
from app.web.templates_engine import templates

router = APIRouter(tags=["journal"])


@router.get("/journal", response_class=HTMLResponse)
async def journal_page(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
) -> HTMLResponse:
    entries = await _list_journal_entries(limit=limit)
    grouped = _group_by_date(entries)

    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "title": "Journal",
            "active_nav": "journal",
            "groups": grouped,
            "total": len(entries),
        },
    )


async def _list_journal_entries(*, limit: int) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT n.screenshot_id, n.body, n.updated_at,
                   s.captured_at, s.thumbnail_path, s.app_name, s.window_title
            FROM screenshot_notes n
            JOIN screenshots s ON s.id = n.screenshot_id
            ORDER BY n.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()

    return [
        {
            "screenshot_id": int(row["screenshot_id"]),
            "body": str(row["body"]),
            "updated_at": parse_iso(str(row["updated_at"])),
            "captured_at": parse_iso(str(row["captured_at"])),
            "thumbnail_path": row["thumbnail_path"],
            "app_name": row["app_name"],
            "window_title": row["window_title"],
        }
        for row in rows
    ]


def _group_by_date(entries: list[dict[str, Any]]) -> OrderedDict[str, list[dict[str, Any]]]:
    out: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for entry in entries:
        captured_at: datetime = entry["captured_at"]
        key = captured_at.astimezone().strftime("%Y-%m-%d")
        out.setdefault(key, []).append(entry)
    return out
