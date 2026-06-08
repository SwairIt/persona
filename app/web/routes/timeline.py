"""Timeline view — newest captures grouped by hour."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.devices import mac_agent_update_prompt
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.models import Screenshot
from app.storage.repository import list_screenshots
from app.web.templates_engine import resolve_display_tz, templates

log = get_logger("persona.grid_sort")

router = APIRouter(tags=["timeline"])

# Whitelist of allowed ``sort_by`` query values mapped to deterministic
# Python ``sorted()`` key functions. Server-side enforcement: any value
# outside this dict falls back to the default (``captured_at``), so no
# user input ever reaches an ORDER BY clause as raw text.
_SORT_OPTIONS: dict[str, tuple[str, bool]] = {
    # key: (attribute name on Screenshot, reverse?)
    "captured_at": ("captured_at", True),
    "captured_at_asc": ("captured_at", False),
    "app_name": ("app_name", False),
    "ocr_length": ("ocr_text", True),
}
_DEFAULT_SORT = "captured_at"


@router.get("/timeline")
async def timeline_alias() -> RedirectResponse:
    """Alias so /timeline (referenced from many places) works."""
    return RedirectResponse(url="/", status_code=303)


@router.get("/", response_class=HTMLResponse, response_model=None)
async def home(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    date: str | None = Query(default=None),
    app: str | None = Query(default=None),
    sort_by: str = Query(default=_DEFAULT_SORT),
    # v1.66 — ``full`` оставлен для back-compat (старые bookmarks/share-link
    # могли содержать ?full=1 чтобы opt-IN в полный layout). Семантически
    # уже no-op потому что mobile-redirect выключен.
    full: int = Query(default=0),  # noqa: ARG001
) -> HTMLResponse | RedirectResponse:
    """Render the main timeline.

    v1.66 — phone-UA auto-redirect к /m выключен. Изначально (v1.30) iPhone
    пользователей тихо бросало на text-only огрызок с одним поисковым полем,
    что выглядело как сломанный сайт. С v1.66 PWA-сетап (180/192/512 иконки,
    safe-area, manifest shortcuts, viewport-fit=cover) делает полный
    timeline отлично работающим на iPhone. /m остаётся доступным как
    осознанный choice — ссылка живёт в hamburger drawer и в footer /timeline,
    но больше не подменяет main flow.
    """
    target_day = _parse_date(date)
    since, until = _day_bounds(target_day)
    sort_key = _coerce_sort(sort_by)

    async with get_connection() as conn:
        total_cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots")
        total_row = await total_cursor.fetchone()
        total_screenshots = int(total_row["n"]) if total_row else 0
        if total_screenshots == 0 and not date:
            return RedirectResponse(url="/welcome", status_code=303)

        shots = await list_screenshots(
            conn,
            limit=500,
            since=since,
            until=until,
            app_name=app,
        )

        apps_for_day = await _day_apps(conn, since, until)

        from app.storage.tags import get_tags_for_many

        tags_by_id = await get_tags_for_many(conn, [s.id for s in shots])

    # ``list_screenshots`` always returns captured_at DESC. When the user
    # requested a different order we re-sort the already-bounded page in
    # Python — cheap (<=500 rows) and keeps the shared repository helper
    # untouched.
    if sort_key != _DEFAULT_SORT:
        shots = _apply_sort(shots, sort_key)
        log.info("grid_sort.timeline", sort_by=sort_key, count=len(shots))

    grouped = _group_by_hour(shots)

    # T29 — Mac-agent update/setup banner (same as /now) so it shows on the
    # timeline home too, which is where the logo and many links land.
    try:
        agent_update = await mac_agent_update_prompt(session["user_id"])
    except Exception as exc:  # never let this break the timeline
        log.warning("timeline.agent_update_check_failed", error=str(exc))
        agent_update = None

    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "title": "Timeline",
            "active_nav": "timeline",
            "agent_update": agent_update,
            "target_day": target_day,
            "prev_day": target_day - timedelta(days=1),
            "next_day": target_day + timedelta(days=1),
            "today": _today(),
            "groups": grouped,
            "total": len(shots),
            "app_filter": app,
            "apps_for_day": apps_for_day,
            "tags_by_id": tags_by_id,
            "sort_by": sort_key,
            "sort_options": _SORT_OPTIONS,
        },
    )


def _coerce_sort(value: str | None) -> str:
    """Reduce arbitrary user input to a whitelisted sort key."""
    if value and value in _SORT_OPTIONS:
        return value
    return _DEFAULT_SORT


def _apply_sort(shots: list[Screenshot], sort_key: str) -> list[Screenshot]:
    """Sort a list of Screenshots using the whitelisted key."""
    attr, reverse = _SORT_OPTIONS[sort_key]

    def key(shot: Screenshot) -> tuple[Any, Any]:
        raw = getattr(shot, attr, None)
        if sort_key == "ocr_length":
            length = len(raw) if isinstance(raw, str) else 0
            return (length, shot.captured_at)
        if raw is None:
            # Empty string sorts before any real value; combined with
            # ``reverse`` this keeps NULLs predictably grouped.
            return ("", shot.captured_at)
        return (raw, shot.captured_at)

    return sorted(shots, key=key, reverse=reverse)


async def _day_apps(conn: Any, since: datetime, until: datetime) -> list[tuple[str, int]]:
    """Return [(app_name, count), ...] for the given day."""
    from app.storage.time import iso

    cursor = await conn.execute(
        "SELECT app_name, COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL "
        "GROUP BY app_name ORDER BY n DESC LIMIT 12",
        (iso(since), iso(until)),
    )
    rows = await cursor.fetchall()
    return [(str(row["app_name"]), int(row["n"])) for row in rows]


def _today() -> datetime:
    now = datetime.now(timezone.utc).astimezone()
    return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)


def _parse_date(value: str | None) -> datetime:
    if not value:
        return _today()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return _today()
    tz = datetime.now().astimezone().tzinfo
    return parsed.replace(tzinfo=tz)


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _group_by_hour(shots: list[Screenshot]) -> OrderedDict[str, list[Screenshot]]:
    # v1.10 fix 2/3 — group header must match the per-card clock. Both
    # paths now resolve the display timezone through the shared helper
    # in :mod:`app.web.templates_engine` (kv ``display_timezone`` if set,
    # otherwise the process-local zone) so a custom MSK render on the
    # cards can't drift away from an unset/UTC header bucket.
    display_tz = resolve_display_tz()
    out: OrderedDict[str, list[Screenshot]] = OrderedDict()
    for shot in shots:
        local = shot.captured_at.astimezone(display_tz)
        key = local.strftime("%H:00")
        out.setdefault(key, []).append(shot)
    return out
