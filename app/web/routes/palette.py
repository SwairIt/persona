"""Command palette data source.

Serves the JSON payload consumed by ``/static/command_palette.js``: a flat list
of jump-to-anywhere items. The static portion covers the ~30 most-used routes;
the dynamic portion enumerates the user's saved searches, auto-collection
rules and tags so the palette tracks the live workspace without a rebuild.

The endpoint deliberately returns an empty/partial payload when a backing
table is missing (e.g. on a fresh install before migrations run): the palette
must never error out the navbar.
"""

from __future__ import annotations

from typing import Any, Final

import aiosqlite
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.palette")

router = APIRouter(tags=["palette"])


# --- Static routes ---------------------------------------------------------

# Curated list of top-level destinations. Order is irrelevant — the client
# sorts by fuzzy-match score — but keep groupings logical for readability.
_STATIC_ROUTES: Final[list[dict[str, str]]] = [
    {"title": "Timeline", "url": "/", "hint": "today's captures"},
    {"title": "Search", "url": "/search", "hint": "full-text + semantic"},
    {"title": "Ask", "url": "/ask", "hint": "AI Q&A over memory"},
    {"title": "Calendar", "url": "/calendar", "hint": "month grid"},
    {"title": "Heatmap", "url": "/heatmap", "hint": "yearly activity"},
    {"title": "Hours", "url": "/hours", "hint": "hour-of-day histogram"},
    {"title": "Focus", "url": "/focus", "hint": "deep work sessions"},
    {"title": "Streak", "url": "/streak", "hint": "consecutive-day chain"},
    {"title": "Vault", "url": "/vault", "hint": "encrypted private notes"},
    {"title": "Audit", "url": "/audit", "hint": "destructive-action log"},
    {"title": "Settings", "url": "/settings"},
    {"title": "Backup", "url": "/settings/backup", "hint": "export & restore"},
    {"title": "Health", "url": "/health", "hint": "liveness probe"},
    {"title": "Doctor", "url": "/doctor", "hint": "diagnostics"},
    {"title": "Journal", "url": "/journal", "hint": "daily notes"},
    {"title": "Reading", "url": "/reading", "hint": "read-later queue"},
    {"title": "Reminders", "url": "/reminders"},
    {"title": "Topics", "url": "/topics", "hint": "AI-clustered themes"},
    {"title": "Tags", "url": "/tags", "hint": "manual labels"},
    {"title": "Saved searches", "url": "/searches"},
    {"title": "Auto-collections", "url": "/collections", "hint": "tag rules"},
    {"title": "Apps", "url": "/apps", "hint": "per-application stats"},
    {"title": "Stats", "url": "/stats"},
    {"title": "Storage report", "url": "/storage", "hint": "disk usage"},
    {"title": "Time-sheet", "url": "/timesheet"},
    {"title": "Weekly digest", "url": "/digest/weekly"},
    {"title": "Daily digests", "url": "/digest/daily"},
    {"title": "Inbox", "url": "/inbox"},
    {"title": "Whitelist", "url": "/whitelist", "hint": "process deny list"},
    {"title": "Help", "url": "/help", "hint": "shortcuts & tips"},
]


# --- Dynamic helpers -------------------------------------------------------


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    """Cheap existence check — keeps the palette working pre-migrations."""
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    )
    row = await cursor.fetchone()
    return row is not None


async def _load_saved_searches(conn: aiosqlite.Connection) -> list[dict[str, str]]:
    if not await _table_exists(conn, "saved_search"):
        return []
    cursor = await conn.execute(
        "SELECT slug, title FROM saved_search ORDER BY created_at DESC LIMIT 50"
    )
    rows = await cursor.fetchall()
    return [
        {
            "title": str(row["title"]),
            "url": f"/searches/{row['slug']}",
            "hint": "saved search",
        }
        for row in rows
    ]


async def _load_auto_collections(conn: aiosqlite.Connection) -> list[dict[str, str]]:
    if not await _table_exists(conn, "auto_collection"):
        return []
    cursor = await conn.execute(
        "SELECT slug, title, tag FROM auto_collection "
        "ORDER BY created_at DESC LIMIT 50"
    )
    rows = await cursor.fetchall()
    return [
        {
            "title": str(row["title"]),
            "url": f"/collection/{row['slug']}",
            "hint": f"#{row['tag']}",
        }
        for row in rows
    ]


async def _load_tags(conn: aiosqlite.Connection) -> list[dict[str, str]]:
    if not await _table_exists(conn, "tags"):
        return []
    cursor = await conn.execute(
        "SELECT t.name AS name, COUNT(st.screenshot_id) AS n "
        "FROM tags t LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
        "GROUP BY t.id, t.name "
        "HAVING n > 0 "
        "ORDER BY n DESC, t.name ASC "
        "LIMIT 50"
    )
    rows = await cursor.fetchall()
    return [
        {
            "title": f"#{row['name']}",
            "url": f"/tags/{row['name']}",
            "hint": f"{int(row['n'])} shots",
        }
        for row in rows
    ]


# --- Endpoint --------------------------------------------------------------


@router.get("/api/palette.json", response_class=JSONResponse)
async def palette_data() -> JSONResponse:
    """Return the merged static + dynamic item list for the command palette."""
    items: list[dict[str, Any]] = [
        {**route, "kind": "route"} for route in _STATIC_ROUTES
    ]

    try:
        async with get_connection() as conn:
            for item in await _load_saved_searches(conn):
                items.append({**item, "kind": "saved"})
            for item in await _load_auto_collections(conn):
                items.append({**item, "kind": "collection"})
            for item in await _load_tags(conn):
                items.append({**item, "kind": "tag"})
    except (aiosqlite.Error, OSError) as exc:
        # Surfaced as a log line, but never breaks the navbar.
        log.warning("palette.dynamic_load_failed", error=str(exc))

    log.info("palette.served", count=len(items))
    return JSONResponse({"items": items})
