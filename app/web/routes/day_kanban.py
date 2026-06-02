"""Day kanban — group one day's screenshots into per-app columns.

Renders a Trello-style board where each column corresponds to a single
``app_name`` observed on the requested day, populated with a vertical strip
of thumbnails ordered newest-first within the column. Columns themselves
are ordered by capture count descending (most-used app first); ties break
alphabetically so the layout is stable when two apps draw the same.

Two endpoints share the same column-building logic so the JSON sibling
(``/api/kanban/{day}.json``) always matches what the HTML page renders:

* ``GET /kanban/{day}`` - HTML page extending ``base.html``.
* ``GET /api/kanban/{day}.json`` - machine-readable equivalent for the
  bookmarklet, command palette, or any future automation.

Both routes default to *today* (local wall-clock day) when the path
component is missing or malformed — same forgiving behaviour as the
day scrubber route, because the natural user reaction to a typo is to
see *something* useful rather than a 400.

This view is read-only: drag-to-reorder is intentionally out of scope
(per the feature spec). The HTML is decorative, the JSON is the truth.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso as _iso
from app.storage.time import parse_iso as _parse_iso
from app.web.routes.thumbnails import thumbnail_url
from app.web.templates_engine import templates

log = get_logger("persona.kanban")

router = APIRouter(tags=["kanban"])

# Hard ceiling on shots per day. A second-by-second day stays well below
# this; the cap keeps the kanban DOM from exploding into thousands of
# <img> nodes which would blow up browser memory on a low-end laptop.
_MAX_SHOTS_PER_DAY = 5_000

# Per-column thumbnail cap — beyond this the strip becomes useless visual
# noise and the in-card scroll gets miserable. Excess shots are still
# counted in the column header so the user knows there's more.
_MAX_THUMBS_PER_COLUMN = 200

# Bucket name for screenshots whose ``app_name`` is NULL or empty. Kept
# as a module constant so the HTML and JSON agree on the exact string.
_UNKNOWN_APP_LABEL = "Unknown"


def _today_local() -> date:
    """Local-date "today" — matches what the wall clock and other day-views show."""
    return datetime.now().astimezone().date()


def _parse_day_or_today(day: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to local today on any failure.

    Matches the day-scrubber convention: a bad path lands on today rather
    than 400-ing. A kanban board is exploratory — punishing a typo is
    user-hostile.
    """
    if day is None or day == "":
        return _today_local()
    try:
        return datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        log.info("kanban.day_invalid_fallback_today", value=day)
        return _today_local()


def _day_bounds_utc(day_value: date) -> tuple[datetime, datetime]:
    """Translate a local calendar day to half-open ``[since_utc, until_utc)``."""
    tz = datetime.now().astimezone().tzinfo
    since_local = datetime(day_value.year, day_value.month, day_value.day, tzinfo=tz)
    until_local = since_local + timedelta(days=1)
    return since_local.astimezone(UTC), until_local.astimezone(UTC)


async def _load_columns(day_value: date) -> list[dict[str, Any]]:
    """Build the kanban columns for ``day_value``.

    Pulls every screenshot whose ``captured_at`` falls in the local day
    window, groups by ``app_name``, and emits one column per distinct app.
    Within a column shots are sorted newest-first — the user typically
    cares about *the most recent thing they did in that app today*, so
    that sits at the top of the strip.

    Parametrised SQL is used unconditionally even though the bounds are
    server-derived; routing every query through ``?`` placeholders means
    no future contributor can accidentally land a string-concat SQL bug
    here.
    """
    since_dt, until_dt = _day_bounds_utc(day_value)

    rows: list[dict[str, Any]]
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, captured_at, app_name, thumbnail_path
            FROM screenshots
            WHERE captured_at >= ?
              AND captured_at < ?
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (_iso(since_dt), _iso(until_dt), _MAX_SHOTS_PER_DAY),
        )
        fetched = await cursor.fetchall()
        # Materialise into plain dicts so we can release the SQLite cursor
        # before doing any per-row Python work (thumbnail URL resolution
        # below touches settings + filesystem).
        rows = [dict(r) for r in fetched]

    # Group by app_name. NULL / empty becomes _UNKNOWN_APP_LABEL so the
    # column has a stable, human-readable header instead of "None".
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw_name = row.get("app_name")
        has_name = raw_name is not None and str(raw_name).strip() != ""
        app_name = str(raw_name) if has_name else _UNKNOWN_APP_LABEL

        thumb_path = row.get("thumbnail_path")
        thumb_url = thumbnail_url(thumb_path) if thumb_path else None

        # Parse the stored ISO timestamp back into a datetime so the
        # template can format it via the standard ``clock`` filter; emit
        # it as ISO again in the dict for consistency with other day-views.
        try:
            captured_at_dt = _parse_iso(str(row["captured_at"]))
        except (ValueError, KeyError):
            # Defensive: a corrupt row should not nuke the whole page.
            continue

        grouped.setdefault(app_name, []).append(
            {
                "id": int(row["id"]),
                "captured_at": _iso(captured_at_dt),
                "captured_at_dt": captured_at_dt,
                "thumbnail_url": thumb_url,
            }
        )

    # Materialise columns. Order: count desc, app_name asc (case-insensitive)
    # so the busiest app sits leftmost and ties break predictably.
    columns: list[dict[str, Any]] = []
    for app_name, shots in grouped.items():
        total = len(shots)
        # Trim the thumbnail strip but keep the *total* count for the header
        # so the user can see "523 shots" even when only 200 are rendered.
        visible = shots[:_MAX_THUMBS_PER_COLUMN]
        columns.append(
            {
                "app_name": app_name,
                "count": total,
                "shots": visible,
            }
        )

    columns.sort(key=lambda c: (-int(c["count"]), str(c["app_name"]).casefold()))
    return columns


def _columns_for_json(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip non-JSON-serialisable helpers (e.g. raw datetimes) from columns.

    The HTML template uses ``captured_at_dt`` to format clock times via the
    Jinja ``clock`` filter; the JSON API consumers only want ISO strings,
    and ``JSONResponse`` would choke on the bare ``datetime`` otherwise.
    """
    out: list[dict[str, Any]] = []
    for col in columns:
        shots_json: list[dict[str, Any]] = []
        for shot in col["shots"]:
            shots_json.append(
                {
                    "id": shot["id"],
                    "captured_at": shot["captured_at"],
                    "thumbnail_url": shot["thumbnail_url"],
                }
            )
        out.append(
            {
                "app_name": col["app_name"],
                "count": col["count"],
                "shots": shots_json,
            }
        )
    return out


@router.get("/kanban/{day}", response_class=HTMLResponse)
async def kanban_page(request: Request, day: str) -> HTMLResponse:
    """Render the per-app kanban view for ``day`` (YYYY-MM-DD)."""
    day_value = _parse_day_or_today(day)
    columns = await _load_columns(day_value)

    total_shots = sum(int(c["count"]) for c in columns)
    log.info(
        "kanban.render",
        day=day_value.isoformat(),
        columns=len(columns),
        shots=total_shots,
        requested=day,
    )

    return templates.TemplateResponse(
        request,
        "day_kanban.html",
        {
            "title": f"Kanban · {day_value.isoformat()}",
            "active_nav": "timeline",
            "day": day_value.isoformat(),
            "prev_day": (day_value - timedelta(days=1)).isoformat(),
            "next_day": (day_value + timedelta(days=1)).isoformat(),
            "today": _today_local().isoformat(),
            "columns": columns,
            "column_count": len(columns),
            "total_shots": total_shots,
        },
    )


@router.get("/kanban", response_class=HTMLResponse)
async def kanban_today(request: Request) -> HTMLResponse:
    """Convenience entry — ``/kanban`` (no day) routes to today.

    FastAPI does not allow optional path params, so we expose a sibling
    handler instead of fighting the router. Matches the day-scrubber
    pattern so the two day-views share an identical UX contract.
    """
    return await kanban_page(request, _today_local().isoformat())


@router.get("/api/kanban/{day}.json", response_class=JSONResponse)
async def kanban_json(day: str) -> JSONResponse:
    """Return the day's columns as JSON — identical structure to the page.

    Keeping the JSON shape pinned to the spec — ``{columns: [{app_name,
    count, shots: [{id, captured_at, thumbnail_url}]}]}`` — means the
    bookmarklet / command palette / any external client can pivot off
    this endpoint without rescraping HTML.
    """
    day_value = _parse_day_or_today(day)
    columns = await _load_columns(day_value)
    payload = _columns_for_json(columns)
    total_shots = sum(int(c["count"]) for c in payload)

    log.info(
        "kanban.json",
        day=day_value.isoformat(),
        columns=len(payload),
        shots=total_shots,
        requested=day,
    )

    return JSONResponse(
        {
            "day": day_value.isoformat(),
            "column_count": len(payload),
            "total_shots": total_shots,
            "columns": payload,
        }
    )


__all__ = ["router"]
