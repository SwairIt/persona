"""Search facets — JSON endpoint for app/tag/date dropdowns on /search.

Used by the autocomplete ``<datalist>`` on the search page and by HTMX
refreshes that want to repopulate the filter panel without a full reload.
All SQL uses bind parameters; no user input is interpolated into queries.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.search.facets")

router = APIRouter(tags=["search-facets"])

# Hard caps so a single request can't ask the DB to serialise the entire
# tag / app universe back to the browser. These match what the dropdown
# actually shows; raise via query params (still bounded) if you need more.
_APP_LIMIT_DEFAULT = 50
_APP_LIMIT_MAX = 500
_TAG_LIMIT_DEFAULT = 200
_TAG_LIMIT_MAX = 2000


@router.get("/api/search/facets.json", response_class=JSONResponse)
async def search_facets(
    app_limit: int = Query(default=_APP_LIMIT_DEFAULT, ge=1, le=_APP_LIMIT_MAX),
    tag_limit: int = Query(default=_TAG_LIMIT_DEFAULT, ge=1, le=_TAG_LIMIT_MAX),
) -> JSONResponse:
    """Return the facet payload used to populate the /search filter panel.

    Shape::

        {
          "apps":     [{"name": "Chrome",  "count": 1234}, ...],
          "tags":     [{"name": "work",    "count":   42}, ...],
          "date_min": "2026-01-04" | null,
          "date_max": "2026-06-01" | null
        }
    """
    apps: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []
    date_min: str | None = None
    date_max: str | None = None

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name AS name, COUNT(*) AS n FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC, app_name ASC LIMIT ?",
            (app_limit,),
        )
        apps = [
            {"name": str(row["name"]), "count": int(row["n"])} for row in await cursor.fetchall()
        ]

        cursor = await conn.execute(
            "SELECT t.name AS name, COUNT(st.screenshot_id) AS n "
            "FROM tags t LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
            "GROUP BY t.id, t.name ORDER BY n DESC, t.name ASC LIMIT ?",
            (tag_limit,),
        )
        tags = [
            {"name": str(row["name"]), "count": int(row["n"] or 0)}
            for row in await cursor.fetchall()
        ]

        cursor = await conn.execute(
            "SELECT MIN(DATE(captured_at)) AS dmin, MAX(DATE(captured_at)) AS dmax FROM screenshots"
        )
        row = await cursor.fetchone()
        if row is not None:
            date_min = str(row["dmin"]) if row["dmin"] is not None else None
            date_max = str(row["dmax"]) if row["dmax"] is not None else None

    log.debug(
        "search.facets.served",
        apps=len(apps),
        tags=len(tags),
        date_min=date_min,
        date_max=date_max,
    )

    return JSONResponse(
        {
            "apps": apps,
            "tags": tags,
            "date_min": date_min,
            "date_max": date_max,
        }
    )
