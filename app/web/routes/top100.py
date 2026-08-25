"""Top-100 dashboard — single page with apps / tags / words / hours / weekdays.

v0.96 feature 1/3.

A read-only, parameter-free overview that compresses the whole capture
history into five "leaderboards" of at most 100 entries each. Heavy
SQL is delegated where a dedicated helper already exists (``top_keywords``
for the OCR + notes word counter); the remaining four columns are
straight aggregate queries against the ``screenshots`` / ``tags`` tables
with parametrised SQL — no string interpolation of user input touches
the database.

The page is intentionally read-only and has no filters: this is the
"zoom-out" view; date-windowed deep-dives already exist on the
per-feature pages (``/apps``, ``/tags``, ``/keywords``, ``/hours``).
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.keywords import top_keywords
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.top100")

router = APIRouter(tags=["top100"])

# Hard ceiling for every leaderboard. The page name is the contract —
# bumping this past 100 would mismatch the URL semantics, so the cap is
# applied uniformly to all five columns even when the underlying helper
# could return more.
_TOP_N: Final[int] = 100

# Whole-history look-back for the word counter. ``top_keywords`` clamps
# internally and computes from a trailing window in days, so we pass the
# largest sensible value (~27 years) to approximate "all time" without
# adding a second code path inside the helper.
_ALL_HISTORY_DAYS: Final[int] = 10_000

# Cosmetic labels for the weekday column. ``strftime('%w', ...)``
# returns ``'0'`` for Sunday through ``'6'`` for Saturday; we display
# Monday-first for the typical European reading order while preserving
# the SQL-native numbering as the sort key.
_WEEKDAY_NAMES: Final[dict[int, str]] = {
    0: "Sunday",
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
}


async def _top_apps(limit: int) -> list[dict[str, int | str]]:
    """Return the ``limit`` most-captured apps across the whole history.

    Apps with a ``NULL`` / empty name are excluded so the column never
    surfaces an unlabelled row.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name AS name, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name "
            "ORDER BY n DESC, app_name ASC "
            "LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    return [{"name": str(row["name"]), "count": int(row["n"])} for row in rows]


async def _top_tags(limit: int) -> list[dict[str, int | str]]:
    """Return the ``limit`` most-applied tags across the whole history.

    Counts pivot through ``screenshot_tags`` so a tag attached to ten
    screenshots scores ``10`` even when its own row is one entry.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT t.name AS name, COUNT(*) AS n "
            "FROM tags AS t "
            "JOIN screenshot_tags AS st ON st.tag_id = t.id "
            "GROUP BY t.id "
            "ORDER BY n DESC, t.name ASC "
            "LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    return [{"name": str(row["name"]), "count": int(row["n"])} for row in rows]


async def _top_hours(limit: int) -> list[dict[str, int | str]]:
    """Return up to ``limit`` hours-of-day ranked by capture volume.

    There are only 24 hour-of-day buckets so ``limit`` is effectively a
    no-op above 24 — we still pass it through for symmetry with the
    other columns. Empty hours are dropped so the column doesn't pad
    itself with ``0``-count rows when the database is small.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT CAST(strftime('%H', captured_at) AS INTEGER) AS hr, "
            "COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "GROUP BY hr "
            "ORDER BY n DESC, hr ASC "
            "LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    items: list[dict[str, int | str]] = []
    for row in rows:
        raw_hour = row["hr"]
        if raw_hour is None:
            continue
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):
            log.warning("top100.bad_hour_skipped", hour=str(raw_hour))
            continue
        if not 0 <= hour < 24:
            log.warning("top100.hour_out_of_range_skipped", hour=hour)
            continue
        items.append({"name": f"{hour:02d}:00", "count": int(row["n"])})
    return items


async def _top_weekdays(limit: int) -> list[dict[str, int | str]]:
    """Return up to ``limit`` weekdays ranked by capture volume.

    There are only 7 weekday buckets so ``limit`` is effectively a
    no-op above 7 — we still pass it through for symmetry. SQLite's
    ``strftime('%w', ...)`` returns ``'0'`` (Sunday) through ``'6'``
    (Saturday); we map to human names for display.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT CAST(strftime('%w', captured_at) AS INTEGER) AS wd, "
            "COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "GROUP BY wd "
            "ORDER BY n DESC, wd ASC "
            "LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    items: list[dict[str, int | str]] = []
    for row in rows:
        raw_wd = row["wd"]
        if raw_wd is None:
            continue
        try:
            wd = int(raw_wd)
        except (TypeError, ValueError):
            log.warning("top100.bad_weekday_skipped", weekday=str(raw_wd))
            continue
        name = _WEEKDAY_NAMES.get(wd)
        if name is None:
            log.warning("top100.weekday_out_of_range_skipped", weekday=wd)
            continue
        items.append({"name": name, "count": int(row["n"])})
    return items


async def _top_words(limit: int) -> list[dict[str, int | str]]:
    """Delegate to :func:`top_keywords` over the whole-history window.

    Re-using the existing helper means stopwords, tokenisation rules
    and the OCR + notes source list stay in a single place.
    """
    raw = await top_keywords(days=_ALL_HISTORY_DAYS, top_n=limit)
    return [{"name": str(item["word"]), "count": int(item["count"])} for item in raw]


@router.get("/stats/top100", response_class=HTMLResponse)
async def top100_page(request: Request) -> HTMLResponse:
    """Render the five-column top-100 leaderboard page."""
    apps = await _top_apps(_TOP_N)
    tags = await _top_tags(_TOP_N)
    words = await _top_words(_TOP_N)
    hours = await _top_hours(_TOP_N)
    weekdays = await _top_weekdays(_TOP_N)

    # NB: the per-column list is keyed ``entries``, NOT ``items``. Jinja
    # resolves ``column.items`` with ``getattr`` FIRST, so on a plain dict it
    # returns the bound ``dict.items`` method instead of our list — the page
    # died with "'builtin_function_or_method' object is not iterable" on the
    # very first ``| map(attribute='count')``. Renaming the key removes the
    # collision instead of forcing every template line to use ``column['items']``.
    columns = [
        {
            "key": "apps",
            "title": "Apps",
            "subtitle": "Most-captured application windows",
            "entries": apps,
            "value_link": "/apps/{name}",
        },
        {
            "key": "tags",
            "title": "Tags",
            "subtitle": "Most-applied tag labels",
            "entries": tags,
            "value_link": None,
        },
        {
            "key": "words",
            "title": "Words",
            "subtitle": "Most-frequent OCR + note tokens",
            "entries": words,
            "value_link": "/search?q={name}",
        },
        {
            "key": "hours",
            "title": "Hours of day",
            "subtitle": "Busiest hour-of-day buckets",
            "entries": hours,
            "value_link": None,
        },
        {
            "key": "weekdays",
            "title": "Weekdays",
            "subtitle": "Busiest day-of-week buckets",
            "entries": weekdays,
            "value_link": None,
        },
    ]

    log.info(
        "top100.rendered",
        apps=len(apps),
        tags=len(tags),
        words=len(words),
        hours=len(hours),
        weekdays=len(weekdays),
        top_n=_TOP_N,
    )

    return templates.TemplateResponse(
        request,
        "top100.html",
        {
            "title": "Top-100 dashboard",
            "active_nav": "stats",
            "columns": columns,
            "top_n": _TOP_N,
        },
    )
