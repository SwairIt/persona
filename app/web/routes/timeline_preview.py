"""Timeline scrubber preview-on-hover endpoint.

Powers the floating thumbnail tooltip that appears when the user hovers a
position on the day-timeline bar. Given a calendar day plus an HH:MM
position, returns the screenshot whose ``captured_at`` is closest to that
local-time target — within a ``+/- 5 minute`` window — so the preview
chip always tracks an actually captured frame rather than interpolating
between gaps.

Distinct from :mod:`app.web.routes.day_scrubber`, which serves a *list* of
frames for full playback; this endpoint serves a single best-match row for
a hover, and is hot-pathed enough that the SQL stays tight (one indexed
range scan, one ``MIN(ABS(...))`` pick).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso as _iso
from app.web.routes.thumbnails import thumbnail_url

log = get_logger("persona.timeline_preview")

router = APIRouter(prefix="/api/timeline", tags=["timeline-preview"])

# Half-window around the hover target. 5 minutes matches the scrubber's
# bucket width on the timeline bar (typically 5-minute density columns) —
# any wider and we'd routinely show a thumbnail visibly off-position;
# any narrower and gaps in capture (idle/screensaver) would 404 too often.
_WINDOW_MINUTES = 5


@router.get("/preview-at", response_class=JSONResponse)
async def preview_at(
    day: str = Query(..., description="Target day as YYYY-MM-DD (local)."),
    hhmm: str = Query(..., description="Target time as HHMM (24h, local)."),
) -> JSONResponse:
    """Return the screenshot nearest to ``day`` + ``hhmm`` (within +/-5 min).

    Lookup is a single SQL statement: bound a +/-5 minute window around
    the target instant, then ``MIN(ABS(strftime('%s', captured_at) - ?))``
    to pick the absolute-closest row. Returns 404 when no row falls inside
    the window — the JS treats that as "draw no tooltip", which is the
    desired behaviour during gaps in the capture log.
    """
    target_utc = _resolve_target(day, hhmm)
    since = target_utc - timedelta(minutes=_WINDOW_MINUTES)
    until = target_utc + timedelta(minutes=_WINDOW_MINUTES)
    target_unix = int(target_utc.timestamp())

    async with get_connection() as conn:
        cursor = await conn.execute(
            (
                "SELECT id, captured_at, thumbnail_path, app_name, window_title "
                "FROM screenshots "
                "WHERE captured_at >= ? AND captured_at <= ? "
                "ORDER BY ABS(CAST(strftime('%s', captured_at) AS INTEGER) - ?) ASC "
                "LIMIT 1"
            ),
            (_iso(since), _iso(until), target_unix),
        )
        row: Any = await cursor.fetchone()

    if row is None:
        log.info(
            "timeline_preview.miss",
            day=day,
            hhmm=hhmm,
            window_minutes=_WINDOW_MINUTES,
        )
        raise HTTPException(status_code=404, detail="No screenshot in window")

    thumb = thumbnail_url(row["thumbnail_path"]) if row["thumbnail_path"] else None
    if thumb is None:
        # Row exists but its thumbnail file has been retention-evicted.
        # Treat as a miss so the JS doesn't render a broken <img>.
        log.info(
            "timeline_preview.no_thumb",
            day=day,
            hhmm=hhmm,
            shot_id=int(row["id"]),
        )
        raise HTTPException(status_code=404, detail="No thumbnail for nearest shot")

    payload: dict[str, Any] = {
        "shot_id": int(row["id"]),
        "captured_at": str(row["captured_at"]),
        "thumbnail_url": thumb,
        "app_name": row["app_name"],
        "window_title": row["window_title"],
    }
    log.info(
        "timeline_preview.hit",
        day=day,
        hhmm=hhmm,
        shot_id=payload["shot_id"],
    )
    return JSONResponse(payload)


def _resolve_target(day: str, hhmm: str) -> datetime:
    """Combine ``day`` (YYYY-MM-DD) + ``hhmm`` (HHMM) into a UTC datetime.

    The day+time pair is interpreted in the *local* timezone of the host —
    matching how the timeline bar renders its 0..24h axis to the user —
    then normalised to UTC because that's how ``captured_at`` is stored.
    """
    try:
        day_part = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid day") from exc

    if len(hhmm) != 4 or not hhmm.isdigit():
        raise HTTPException(status_code=400, detail="Invalid hhmm")
    try:
        hours = int(hhmm[:2])
        minutes = int(hhmm[2:])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid hhmm") from exc
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise HTTPException(status_code=400, detail="Invalid hhmm")

    tz = datetime.now().astimezone().tzinfo
    local_dt = datetime(
        day_part.year,
        day_part.month,
        day_part.day,
        hours,
        minutes,
        0,
        tzinfo=tz,
    )
    return local_dt.astimezone(UTC)
