"""Day scrubber — view one calendar day of captures as a scrubbable timeline.

Renders a single big image element backed by a horizontal range slider that
walks chronologically through every screenshot captured on a given day.
Pairs an HTML page (``/scrubber/{day}``) with a JSON sibling
(``/api/scrubber/{day}.json``) for the in-page JS to consume — same shape,
same ordering, same time-window math, so the page never desynchronises.

Distinct from :mod:`app.web.routes.range_timeline` (grid of cards) and
:mod:`app.web.routes.diff_slider` (two-image comparison): the scrubber treats
a day as a *video* — one frame at a time, ordered ascending, with autoplay
and arrow-key stepping.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.storage.time import iso as _iso
from app.web.routes.thumbnails import thumbnail_url
from app.web.templates_engine import templates

log = get_logger("persona.scrubber")

router = APIRouter(tags=["scrubber"])

# Hard ceiling on frames per day. A captured-every-second day still fits well
# under this — we keep the cap so the page never tries to render a slider with
# tens of thousands of stops, which would freeze the browser thread on input.
_MAX_FRAMES_PER_DAY = 5_000


def _today_local() -> date:
    """Local-date "today" — matches what the user sees on the wall clock."""
    return datetime.now().astimezone().date()


def _parse_day_or_today(day: str | None) -> date:
    """Parse a YYYY-MM-DD string; fall back to local "today" if absent/invalid.

    The task contract says "defaults to today if day param missing or invalid"
    — so unlike :mod:`app.web.routes.range_timeline` we do *not* raise on a bad
    string. A typo in the URL silently lands on today's view, which is the
    most useful behaviour for a scrubber (the user just wants frames).
    """
    if day is None or day == "":
        return _today_local()
    try:
        return datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        log.info("scrubber.day_invalid_fallback_today", value=day)
        return _today_local()


def _day_bounds_utc(day_value: date) -> tuple[datetime, datetime]:
    """Translate a local calendar day to (since_utc, until_utc).

    Matches the half-open ``[since, until)`` window the repository's
    ``list_screenshots`` expects, where ``until`` is midnight of the next day
    so the entire target day is included.
    """
    tz = datetime.now().astimezone().tzinfo
    since_local = datetime(day_value.year, day_value.month, day_value.day, tzinfo=tz)
    until_local = since_local + timedelta(days=1)
    return since_local.astimezone(UTC), until_local.astimezone(UTC)


async def _load_frames(day_value: date) -> list[dict[str, Any]]:
    """Fetch all screenshots for ``day_value`` ordered oldest-first.

    The repository returns DESC by ``captured_at`` — we reverse here because
    a scrubber reads time-forward (left = earliest, right = newest).
    """
    since_dt, until_dt = _day_bounds_utc(day_value)
    async with get_connection() as conn:
        shots = await list_screenshots(
            conn,
            limit=_MAX_FRAMES_PER_DAY,
            since=since_dt,
            until=until_dt,
        )

    shots.sort(key=lambda s: s.captured_at)

    frames: list[dict[str, Any]] = []
    for shot in shots:
        thumb = thumbnail_url(shot.thumbnail_path) if shot.thumbnail_path else None
        if thumb is None:
            # No thumbnail (retention-evicted or never generated) — skip rather
            # than render a broken <img>, which would stall autoplay on a blank.
            continue
        frames.append(
            {
                "id": shot.id,
                "captured_at": _iso(shot.captured_at),
                "app_name": shot.app_name,
                "thumbnail_url": thumb,
            }
        )
    return frames


@router.get("/scrubber/{day}", response_class=HTMLResponse)
async def scrubber_page(request: Request, day: str) -> HTMLResponse:
    """Render the day scrubber for ``day`` (YYYY-MM-DD, defaults to today)."""
    day_value = _parse_day_or_today(day)
    frames = await _load_frames(day_value)
    log.info(
        "scrubber.render",
        day=day_value.isoformat(),
        frames=len(frames),
        requested=day,
    )
    return templates.TemplateResponse(
        request,
        "day_scrubber.html",
        {
            "title": f"Scrubber · {day_value.isoformat()}",
            "active_nav": "timeline",
            "day": day_value.isoformat(),
            "prev_day": (day_value - timedelta(days=1)).isoformat(),
            "next_day": (day_value + timedelta(days=1)).isoformat(),
            "today": _today_local().isoformat(),
            "frames": frames,
            "frame_count": len(frames),
        },
    )


@router.get("/scrubber", response_class=HTMLResponse)
async def scrubber_today(request: Request) -> HTMLResponse:
    """Convenience entry — ``/scrubber`` (no day) routes to today.

    The path-param variant cannot be omitted in FastAPI, so we add a sibling
    handler instead of fighting the router. Keeps the URL clean for the
    "open the scrubber on today" use case from the nav / palette.
    """
    return await scrubber_page(request, _today_local().isoformat())


@router.get("/api/scrubber/{day}.json", response_class=JSONResponse)
async def scrubber_json(day: str) -> JSONResponse:
    """Return the day's frames as JSON — identical payload to the page context.

    The HTML page already embeds the list, but we expose this endpoint so a
    client (e.g. the browser extension or a future SPA) can reuse the same
    server-side ordering + thumbnail-URL logic without rescraping HTML.
    """
    day_value = _parse_day_or_today(day)
    frames = await _load_frames(day_value)
    log.info(
        "scrubber.json",
        day=day_value.isoformat(),
        frames=len(frames),
        requested=day,
    )
    return JSONResponse(
        {
            "day": day_value.isoformat(),
            "count": len(frames),
            "frames": frames,
        }
    )
