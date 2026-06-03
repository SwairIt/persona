"""v1.7 feature 2/3 — ``/admin/collection-visits`` dashboard.

Pairs with the new ``collection_visit`` table (migration 088) and the
write path in :mod:`app.web.routes.auto_collections`. Each successful
render of a public ``/collection/{slug}`` page writes one row; this
module rolls those rows up into two views:

* a **per-slug leaderboard** — total visits in the last
  :data:`_DEFAULT_DAYS` days, the most-recent visit per slug, the rule's
  underlying tag, and whether the rule is still ``public`` (private
  rules can still accrue visits from the loopback owner) — joined back
  to ``auto_collection`` so deleted rules render with placeholders;
* a **30-day visit timeline** rendered as a tiny inline SVG line chart,
  zero-filled on missing days so the x-axis is uniform.

All SQL is parametrised. The window length flows in as a bound argument
to SQLite's ``datetime('now', ?)`` modifier and is clamped to ``[1,
365]`` at the FastAPI layer so an out-of-range query string returns 422
before the aggregator runs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

import aiosqlite
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["collection-visit-stats"])
log = get_logger("persona.collection.visits")

# Window bounds. The default of 30 days matches the user-facing brief
# ("show per-collection stats … 30-day SVG line"). The clamp range
# mirrors :mod:`app.share_analytics` so the operator gets a predictable
# feel across the admin dashboards.
_DEFAULT_DAYS = 30
_MIN_DAYS = 1
_MAX_DAYS = 365

# How many rows to surface in the per-slug leaderboard. Kept small so
# the Tailwind table stays scannable on a typical 13-inch laptop; an
# operator who needs more drill-down can extend the JSON sibling.
_TOP_SLUGS_LIMIT = 50


class SlugRow(TypedDict):
    """One row in the per-slug aggregate.

    ``title`` / ``tag`` / ``public`` come from the LEFT JOIN onto
    ``auto_collection`` and may be ``None`` when the rule has since been
    deleted — we deliberately keep the journal FK-free so visit history
    survives a rule drop. ``last_visit`` is the most recent visit
    timestamp in the window (UTC, ``YYYY-MM-DD HH:MM:SS``).
    """

    slug: str
    title: str | None
    tag: str | None
    public: int | None
    visits: int
    last_visit: str


class DailyVisits(TypedDict):
    """One day's visit count, oldest first when listed by :func:`_compute`."""

    date: str
    visits: int


class CollectionVisitStats(TypedDict):
    """Composite payload returned by :func:`_compute`."""

    by_slug: list[SlugRow]
    daily: list[DailyVisits]


def _clamp_days(days: int) -> int:
    """Clamp ``days`` to ``[_MIN_DAYS, _MAX_DAYS]`` — see module docstring."""
    return max(_MIN_DAYS, min(int(days), _MAX_DAYS))


async def _fetch_by_slug(
    conn: aiosqlite.Connection, since_modifier: str
) -> list[SlugRow]:
    """Per-slug roll-up, LEFT-joined to ``auto_collection`` for metadata.

    Ordered by visit count desc, slug asc — stable for the leaderboard.
    """
    cursor = await conn.execute(
        """
        SELECT cv.slug              AS slug,
               ac.title             AS title,
               ac.tag               AS tag,
               ac.public            AS public,
               COUNT(*)             AS visits,
               MAX(cv.visited_at)   AS last_visit
        FROM collection_visit AS cv
        LEFT JOIN auto_collection AS ac ON ac.slug = cv.slug
        WHERE cv.visited_at >= datetime('now', ?)
        GROUP BY cv.slug
        ORDER BY visits DESC, cv.slug ASC
        LIMIT ?
        """,
        (since_modifier, _TOP_SLUGS_LIMIT),
    )
    rows = await cursor.fetchall()
    out: list[SlugRow] = []
    for row in rows:
        public_raw = row["public"]
        out.append(
            SlugRow(
                slug=str(row["slug"]),
                title=(str(row["title"]) if row["title"] is not None else None),
                tag=(str(row["tag"]) if row["tag"] is not None else None),
                public=(int(public_raw) if public_raw is not None else None),
                visits=int(row["visits"]),
                last_visit=str(row["last_visit"]),
            )
        )
    return out


async def _fetch_daily(
    conn: aiosqlite.Connection, since_modifier: str
) -> dict[str, int]:
    """Visits-per-day in the window, keyed by ``YYYY-MM-DD``.

    Returned as a dict so the caller can zero-fill missing days against
    a Python-side axis — SQLite has no ``generate_series`` equivalent.
    """
    cursor = await conn.execute(
        """
        SELECT DATE(visited_at) AS day, COUNT(*) AS visits
        FROM collection_visit
        WHERE visited_at >= datetime('now', ?)
        GROUP BY day
        ORDER BY day ASC
        """,
        (since_modifier,),
    )
    rows = await cursor.fetchall()
    return {str(row["day"]): int(row["visits"]) for row in rows}


async def _compute(days: int) -> CollectionVisitStats:
    """Roll up the last ``days`` days of ``collection_visit`` rows.

    Single payload feeds both the HTML page and its JSON sibling so the
    two endpoints can never drift.
    """
    capped = _clamp_days(days)
    since_modifier = f"-{capped} days"

    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=capped - 1)

    async with get_connection() as conn:
        by_slug = await _fetch_by_slug(conn, since_modifier)
        by_day = await _fetch_daily(conn, since_modifier)

    daily: list[DailyVisits] = []
    cursor_day: date = start_day
    while cursor_day <= today:
        key = cursor_day.isoformat()
        daily.append(DailyVisits(date=key, visits=by_day.get(key, 0)))
        cursor_day += timedelta(days=1)

    log.info(
        "collection_visit_stats.computed",
        days=capped,
        slugs=len(by_slug),
        daily_days=len(daily),
        total_visits=sum(row["visits"] for row in daily),
    )

    return CollectionVisitStats(by_slug=by_slug, daily=daily)


@router.get("/admin/collection-visits", response_class=HTMLResponse)
async def collection_visits_page(
    request: Request,
    days: int = Query(_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the per-collection visit dashboard for the last ``days`` days."""
    payload = await _compute(days=days)

    total_visits = sum(row["visits"] for row in payload["daily"])
    max_daily = max((row["visits"] for row in payload["daily"]), default=0)
    max_slug_visits = max((row["visits"] for row in payload["by_slug"]), default=0)

    log.debug(
        "collection_visit_stats.page.rendered",
        days=days,
        total_visits=total_visits,
        slugs=len(payload["by_slug"]),
    )

    return templates.TemplateResponse(
        request,
        "collection_visit_stats.html",
        {
            "title": "Collection visits",
            "active_nav": "stats",
            "days_window": days,
            "by_slug": payload["by_slug"],
            "daily": payload["daily"],
            "total_visits": total_visits,
            "max_daily": max_daily,
            "max_slug_visits": max_slug_visits,
        },
    )


@router.get("/api/collection-visits.json")
async def collection_visits_json(
    days: int = Query(_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the per-collection visit payload as JSON.

    Shape: ``{"days": N, "by_slug": [...], "daily": [...]}``. ``daily``
    is oldest-first with gaps zero-filled, matching the SVG line chart
    on the HTML page.
    """
    payload = await _compute(days=days)
    return JSONResponse(
        {
            "days": days,
            "by_slug": [dict(row) for row in payload["by_slug"]],
            "daily": [dict(row) for row in payload["daily"]],
        }
    )


__all__ = [
    "CollectionVisitStats",
    "DailyVisits",
    "SlugRow",
    "router",
]
