"""Dashboard tile editor — pick which cards appear on /dashboard and in what order.

The /dashboard view (v0.65) historically rendered a hard-coded sequence
of five cards. v0.81 lets the user opt cards in or out and reorder them
without touching the template — the choice lives in a single CSV row
in ``kv_settings`` keyed ``'dashboard_tiles'`` (seeded by migration
``071_dashboard_tiles.sql``).

The CSV shape was chosen instead of JSON to mirror the
"single string-typed kv row" pattern already established by
``compact_mode`` / ``grayscale_mode`` — keeps the read path a one-liner
and a manual kv edit through /settings stays human-friendly.

Security note: tile names come from a user-writable kv row, but the
template uses them to dispatch into a fixed set of ``{% if %}`` branches.
Still, every read goes through :func:`parse_tiles_csv` which intersects
the parsed list with :data:`KNOWN_TILES` — unknown identifiers are
dropped, the default ordering fills in any missing tiles, and duplicates
are collapsed to first occurrence. That keeps the renderer's iteration
list closed even if someone hand-edits the kv row.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

log = get_logger("persona.dashboard.tiles")

router = APIRouter(tags=["dashboard"])

# kv row shared with :mod:`app.web.routes.dashboard` and migration
# ``071_dashboard_tiles.sql`` — single source of truth so a rename here
# can't drift the writer and reader out of sync.
DASHBOARD_TILES_KV_KEY = "dashboard_tiles"

# Whitelist of tile identifiers the renderer knows how to draw. Order
# inside this tuple is the *default* order — used when the kv row is
# missing or when the user's saved list omits a tile, in which case the
# missing tile is appended at its default-order index so a future
# release that adds a new tile shows it without the user having to
# re-save their selection.
KNOWN_TILES: tuple[str, ...] = (
    "today",
    "streak",
    "top_apps",
    "latest_digest",
    "capture_status",
    "pinned",
)

# Human-readable labels for the editor checkbox list. Kept in the route
# layer (not the template) so server-side rendering of the form and the
# whitelist used by :func:`parse_tiles_csv` cannot drift apart.
TILE_LABELS: dict[str, str] = {
    "today": "Today",
    "streak": "Streak",
    "top_apps": "Top apps (7d)",
    "latest_digest": "Latest weekly digest",
    "capture_status": "Capture status",
    "pinned": "Pinned shots",
}


def parse_tiles_csv(raw: str | None) -> list[str]:
    """Parse a CSV kv value into an ordered list of known tile names.

    Unknown identifiers are dropped, duplicates collapse to their first
    occurrence, and any whitelisted tile missing from the parsed list is
    appended at its default-order position so a newly-shipped tile
    appears for existing installs without a manual save.
    """
    if raw is None:
        ordered: list[str] = []
    else:
        seen: set[str] = set()
        ordered = []
        for chunk in raw.split(","):
            name = chunk.strip()
            if not name or name in seen or name not in KNOWN_TILES:
                continue
            seen.add(name)
            ordered.append(name)

    # Append any whitelisted tiles the user hasn't explicitly placed yet
    # so a future release that adds a tile renders without a re-save.
    for default_name in KNOWN_TILES:
        if default_name not in ordered:
            ordered.append(default_name)

    return ordered


def _format_tiles_csv(tiles: list[str]) -> str:
    """Serialise an ordered tile list back to the stored CSV shape."""
    return ",".join(tiles)


async def load_tile_order() -> list[str]:
    """Read the user's tile order from kv_settings.

    Public helper consumed by :mod:`app.web.routes.dashboard` — keeping
    the read path here (rather than duplicating the CSV parse inside the
    dashboard route) means the whitelist + default-fill behaviour stays
    in one place.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, DASHBOARD_TILES_KV_KEY)
    return parse_tiles_csv(raw)


@router.get("/settings/dashboard", response_class=HTMLResponse)
async def dashboard_tiles_editor(request: Request) -> HTMLResponse:
    """Render the tile editor — checkboxes for visibility, arrows for order."""
    async with get_connection() as conn:
        raw = await get_kv(conn, DASHBOARD_TILES_KV_KEY)

    # The saved list is *only* the tiles the user wants visible, in
    # order. Hidden tiles are everything in KNOWN_TILES minus the saved
    # list — surfaced separately so the editor can show them as unticked
    # rows under the ordered list.
    if raw is None:
        visible: list[str] = list(KNOWN_TILES)
    else:
        seen: set[str] = set()
        visible = []
        for chunk in raw.split(","):
            name = chunk.strip()
            if not name or name in seen or name not in KNOWN_TILES:
                continue
            seen.add(name)
            visible.append(name)

    hidden = [name for name in KNOWN_TILES if name not in visible]

    log.info(
        "dashboard.tiles.editor",
        visible=len(visible),
        hidden=len(hidden),
    )

    return templates.TemplateResponse(
        request,
        "dashboard_tiles.html",
        {
            "title": "Dashboard tiles",
            "active_nav": "settings",
            "visible_tiles": visible,
            "hidden_tiles": hidden,
            "tile_labels": TILE_LABELS,
        },
    )


@router.post("/settings/dashboard", response_class=HTMLResponse)
async def dashboard_tiles_save(
    request: Request,
    order: str = Form(default=""),
) -> RedirectResponse:
    """Persist the new tile order to ``kv_settings``.

    The editor posts a single ``order`` field holding the CSV of
    currently-visible tiles in user-selected order — built client-side
    from the checkbox state plus the up/down arrows. Unknown identifiers
    are dropped server-side via :func:`parse_tiles_csv`, then we
    intersect with the *visible* set so a tile the user explicitly
    unticked stays hidden (rather than being re-added by the
    default-fill branch of :func:`parse_tiles_csv`).
    """
    # Preserve the user's stated visible set: anything in the raw CSV
    # (post-whitelist) is what they want shown. We *don't* call
    # :func:`parse_tiles_csv` here because its default-fill would
    # silently re-add tiles the user just unchecked.
    seen: set[str] = set()
    visible: list[str] = []
    for chunk in order.split(","):
        name = chunk.strip()
        if not name or name in seen or name not in KNOWN_TILES:
            continue
        seen.add(name)
        visible.append(name)

    # Edge case: an empty POST (user unticked everything) collapses to
    # the default order so /dashboard never renders a fully-blank page.
    # A blank dashboard is almost certainly a mistake, not an intent.
    if not visible:
        visible = list(KNOWN_TILES)

    async with get_connection() as conn:
        await set_kv(conn, DASHBOARD_TILES_KV_KEY, _format_tiles_csv(visible))

    log.info(
        "dashboard.tiles.save",
        count=len(visible),
        order=",".join(visible),
    )

    return RedirectResponse(url="/settings/dashboard", status_code=303)
