"""Picker UI for choosing two screenshots to feed into /diff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.storage.models import Screenshot
from app.storage.repository import get_screenshot, list_screenshots
from app.web.templates_engine import templates

router = APIRouter(tags=["analysis"])

_MAX_CANDIDATES = 12


@router.get("/diff-picker", response_class=HTMLResponse)
async def diff_picker_page(
    request: Request,
    left: int | None = Query(default=None),
) -> HTMLResponse:
    """Render a picker for two screenshot IDs that submits to /diff.

    If ``left`` is provided, also surface other captures of the same app on
    the same calendar day as quick-pick candidates for the right side.
    """
    left_id: int | None = left
    left_shot: Screenshot | None = None
    candidates: list[Screenshot] = []

    if left_id is not None:
        async with get_connection() as conn:
            left_shot = await get_screenshot(conn, left_id)
            if left_shot is not None and left_shot.app_name:
                start_of_day, end_of_day = _day_bounds(left_shot.captured_at)
                same_day = await list_screenshots(
                    conn,
                    app_name=left_shot.app_name,
                    since=start_of_day,
                    until=end_of_day,
                    limit=_MAX_CANDIDATES + 1,
                )
                candidates = [shot for shot in same_day if shot.id != left_shot.id][
                    :_MAX_CANDIDATES
                ]

    return templates.TemplateResponse(
        request,
        "diff_picker.html",
        {
            "title": "Compare two screenshots",
            "active_nav": "timeline",
            "left_id": left_id,
            "left_shot": left_shot,
            "candidates": candidates,
        },
    )


def _day_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """Return [start_of_day, start_of_next_day) in the same tz as ``moment``."""
    tz = moment.tzinfo or timezone.utc
    local = moment.astimezone(tz)
    start = datetime(local.year, local.month, local.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end
