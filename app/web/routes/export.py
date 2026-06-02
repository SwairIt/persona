"""Day-snapshot export — dump a day of captures as JSON for backup or analysis."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response

from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.storage.time import iso

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/day")
async def export_day(date: str | None = Query(default=None)) -> Response:
    """Return all screenshots for the given day as JSON."""
    target_day = _parse_date(date)
    since = target_day.astimezone(timezone.utc)
    until = (target_day + timedelta(days=1)).astimezone(timezone.utc)

    async with get_connection() as conn:
        shots = await list_screenshots(conn, since=since, until=until, limit=10000)

    payload = {
        "generated_at": iso(datetime.now(timezone.utc)),
        "day": target_day.strftime("%Y-%m-%d"),
        "count": len(shots),
        "screenshots": [
            {
                "id": s.id,
                "captured_at": iso(s.captured_at),
                "app_name": s.app_name,
                "window_title": s.window_title,
                "process_name": s.process_name,
                "width": s.width,
                "height": s.height,
                "phash": s.phash,
                "ocr_status": s.ocr_status,
                "ocr_text": s.ocr_text,
                "thumbnail_path": s.thumbnail_path,
                "dedup_group_id": s.dedup_group_id,
            }
            for s in shots
        ],
    }

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"persona-snapshot-{target_day.strftime('%Y-%m-%d')}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/range")
async def export_range(
    since: str = Query(...),
    until: str = Query(...),
) -> JSONResponse:
    """Return screenshots between two ISO timestamps."""
    since_dt = datetime.fromisoformat(since)
    until_dt = datetime.fromisoformat(until)
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)

    async with get_connection() as conn:
        shots = await list_screenshots(conn, since=since_dt, until=until_dt, limit=50000)

    return JSONResponse(
        {
            "generated_at": iso(datetime.now(timezone.utc)),
            "since": iso(since_dt),
            "until": iso(until_dt),
            "count": len(shots),
            "screenshots": [
                {
                    "id": s.id,
                    "captured_at": iso(s.captured_at),
                    "app_name": s.app_name,
                    "window_title": s.window_title,
                    "ocr_text": s.ocr_text,
                }
                for s in shots
            ],
        }
    )


def _parse_date(value: str | None) -> datetime:
    if not value:
        now = datetime.now().astimezone()
        return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        msg = f"Invalid date format (expected YYYY-MM-DD): {value}"
        raise ValueError(msg) from exc
    return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
