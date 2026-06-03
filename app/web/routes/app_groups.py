"""Admin UI + stats for app-group categorisation.

Two endpoints sit under separate prefixes so they match the navigation
the user expects:

* ``GET  /settings/app-groups`` — table of every app already assigned
  to a group, plus a "suggested" panel of the most-captured raw
  ``app_name`` values that have no group yet. Each row is a self-
  contained form so the operator can change one mapping without
  touching the others.

* ``POST /settings/app-groups`` — upsert one ``(app_name, group_name)``
  pair, 303-redirect back to the form (standard PRG pattern → refresh
  doesn't re-submit). An empty ``group_name`` deletes the row via the
  storage helper, so the same endpoint covers create / update / clear.

* ``GET  /stats/app-groups`` — bar-chart view of
  :func:`app.app_groups.totals_by_group` for a configurable window.
  Renders inline ``<div>`` bars (Tailwind only — no chart library in
  the bundle) so the page degrades to a readable table without JS.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.app_groups import list_all, set_group, totals_by_group
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.app_groups")

router = APIRouter(tags=["app-groups"])

# How many distinct raw ``app_name`` values we surface as "suggested"
# rows on the settings page. Matches the 64 used by app_aliases — that
# limit was chosen to fit on a single laptop screen and keeps both
# admin pages visually consistent.
_SUGGESTION_LIMIT = 64

# Default window for the stats page. Mirrors the docstring of
# :func:`app.app_groups.totals_by_group` so a bare ``/stats/app-groups``
# matches what the helper returns when called with no argument.
_DEFAULT_DAYS = 30

# Hint list the dropdown ships with. The schema accepts any free-form
# string; this is purely a UX nudge so the average user lands on the
# same five buckets across apps rather than inventing micro-categories.
DEFAULT_GROUPS: tuple[str, ...] = ("work", "personal", "comms", "dev", "games")


def _format_duration(seconds: int) -> str:
    """Render an integer second count as ``HhMMm`` / ``MMm SSs`` / ``SSs``.

    Mirrors the convention from :mod:`app.time_on_app`'s templates so a
    user toggling between the two stats pages reads the same units.
    """
    if seconds <= 0:
        return "0s"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


@router.get("/settings/app-groups", response_class=HTMLResponse)
async def groups_page(request: Request) -> HTMLResponse:
    """Render the group-assignment form with current mappings + suggestions.

    Pulls every configured assignment and the top distinct ``app_name``
    values from ``screenshots``. The "suggested" list filters out names
    that already have a group so the operator never sees a duplicate
    row in the two sections.
    """
    assignments = await list_all()
    assigned_apps = {item["app_name"] for item in assignments}
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC LIMIT ?",
            (_SUGGESTION_LIMIT,),
        )
        rows = await cursor.fetchall()
    suggested = [
        {"app_name": str(row["app_name"]), "count": int(row["n"])}
        for row in rows
        if str(row["app_name"]) not in assigned_apps
    ]
    return templates.TemplateResponse(
        request,
        "app_groups.html",
        {
            "title": "App groups",
            "active_nav": "settings",
            "assignments": assignments,
            "suggested": suggested,
            "default_groups": DEFAULT_GROUPS,
        },
    )


@router.post("/settings/app-groups")
async def groups_save(
    app_name: str = Form(...),
    group_name: str = Form(""),
) -> RedirectResponse:
    """Upsert (or clear) one ``(app_name, group_name)`` pair, then 303 back.

    A blank ``group_name`` is the documented "remove" path — the
    storage helper deletes the row, so the same endpoint covers
    create / update / clear without a separate ``DELETE`` route. We
    intentionally do not enforce a controlled vocabulary at this layer:
    the schema accepts any free-form string and so does this handler.
    """
    try:
        await set_group(app_name, group_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/app-groups", status_code=303)


@router.get("/stats/app-groups", response_class=HTMLResponse)
async def groups_stats(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=1, le=365),
) -> HTMLResponse:
    """Render per-group totals over the configurable window.

    The chart is intentionally CSS-only: a horizontal bar per group,
    width = share of the busiest bucket's seconds. That keeps the page
    legible with no JS and matches the visual idiom of
    ``/time-on-app/summary``. Totals also appear as numbers so a
    user piping the page into a screenreader still gets the data.
    """
    totals = await totals_by_group(days=days)
    # ``totals_by_group`` returns ``list[dict[str, object]]`` so the
    # values land in mypy as ``object``; the runtime invariant (set by
    # :class:`GroupTotal`) is that ``total_seconds`` and ``shots`` are
    # always ``int`` and ``group_name`` is always ``str``. The casts
    # below codify that contract without leaking ``object`` into the
    # template context.
    max_seconds = max(
        (int(item["total_seconds"]) for item in totals),  # type: ignore[call-overload]
        default=0,
    )
    enriched: list[dict[str, object]] = []
    for item in totals:
        seconds_val = int(item["total_seconds"])  # type: ignore[call-overload]
        shots_val = int(item["shots"])  # type: ignore[call-overload]
        enriched.append(
            {
                "group_name": str(item["group_name"]),
                "shots": shots_val,
                "total_seconds": seconds_val,
                "duration": _format_duration(seconds_val),
                "percent": (
                    (seconds_val / max_seconds * 100.0)
                    if max_seconds > 0
                    else 0.0
                ),
            }
        )
    total_seconds = sum(int(item["total_seconds"]) for item in enriched)  # type: ignore[call-overload]
    total_shots = sum(int(item["shots"]) for item in enriched)  # type: ignore[call-overload]
    return templates.TemplateResponse(
        request,
        "app_groups_stats.html",
        {
            "title": f"App groups · last {days} days",
            "active_nav": "stats",
            "days": days,
            "items": enriched,
            "total_seconds": total_seconds,
            "total_shots": total_shots,
            "total_duration": _format_duration(total_seconds),
        },
    )
