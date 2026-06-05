"""Smart-dedup admin UI — stats, manual scan, recent groupings (v1.42).

Surface area is deliberately small:

* ``GET  /admin/smart-dedup`` — Tailwind page that shows the marker
  count, the timestamp of the last detector run, a sample of the most
  recent trivial-dup groupings, and a "Run Now" button.
* ``POST /api/smart-dedup/scan`` — fire-and-await one detector pass,
  redirect back to the admin page so the new numbers show up
  immediately.
* ``GET  /api/smart-dedup/stats.json`` — JSON companion exposing the
  same numbers for scripting / health-check consumers.

Every value reaching SQLite travels through a ``?`` placeholder. The
"last run" timestamp is sourced from the most-recent log row written
by :func:`app.smart_dedup.detect_trivial_dups` — we store it in the
``kv_settings`` row ``smart_dedup_last_run_at`` whenever the POST
handler triggers a manual scan so the page can show a useful "ran N
minutes ago" hint even on a fresh install.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.smart_dedup import detect_trivial_dups
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

log = get_logger("persona.web.smart_dedup")

router = APIRouter(tags=["smart-dedup"])

# kv row that stores the ISO-8601 timestamp of the most recent
# detector run triggered via the admin POST endpoint. Decoupled from
# the worker's heartbeat so the page can render "Last run: 3m ago"
# without having to query the worker registry.
_KV_LAST_RUN: Final[str] = "smart_dedup_last_run_at"

# How many recent trivial-dup groupings to show on the admin page.
# Each "grouping" is one anchor + its dependants; we cap the anchor
# list at 25 so the rendered DOM stays manageable even on a busy
# install.
_SAMPLE_ANCHORS: Final[int] = 25

# Per-anchor cap on how many follower rows we show inline. Most
# bundles in the wild are 2-4 shots; 8 is a comfortable upper bound
# for the visible-context paragraph.
_SAMPLE_FOLLOWERS: Final[int] = 8


async def _count_marked() -> int:
    """Return total screenshots currently marked as trivial duplicates."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots WHERE trivial_dup_of_id IS NOT NULL"
        )
        row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _count_anchors() -> int:
    """Return the number of distinct "kept" anchors with at least one dup.

    An anchor is a screenshot id that appears at least once in the
    ``trivial_dup_of_id`` column of another row. The distinct count
    answers "how many bundles do we have on record" — useful as a
    sanity check against the raw marker count.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(DISTINCT trivial_dup_of_id) AS n "
            "FROM screenshots "
            "WHERE trivial_dup_of_id IS NOT NULL"
        )
        row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _fetch_recent_groupings(limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` recent trivial-dup bundles.

    Each entry is a dict::

        {
            "anchor": {id, captured_at, app_name, window_title},
            "followers": [{id, captured_at}, ...],
            "follower_count": int,
        }

    Ordered by the anchor's ``captured_at DESC`` so the operator sees
    the latest bundles first — those are the ones most likely to
    matter for an ongoing capture session.
    """
    async with get_connection() as conn:
        anchor_cursor = await conn.execute(
            "SELECT s.id AS id, "
            "       s.captured_at AS captured_at, "
            "       s.app_name AS app_name, "
            "       s.window_title AS window_title, "
            "       COUNT(d.id) AS follower_count "
            "FROM screenshots s "
            "JOIN screenshots d ON d.trivial_dup_of_id = s.id "
            "GROUP BY s.id "
            "ORDER BY s.captured_at DESC "
            "LIMIT ?",
            (limit,),
        )
        anchor_rows = await anchor_cursor.fetchall()

        groupings: list[dict[str, Any]] = []
        for anchor in anchor_rows:
            follower_cursor = await conn.execute(
                "SELECT id, captured_at "
                "FROM screenshots "
                "WHERE trivial_dup_of_id = ? "
                "ORDER BY captured_at ASC "
                "LIMIT ?",
                (int(anchor["id"]), _SAMPLE_FOLLOWERS),
            )
            follower_rows = await follower_cursor.fetchall()
            groupings.append(
                {
                    "anchor": {
                        "id": int(anchor["id"]),
                        "captured_at": str(anchor["captured_at"]),
                        "app_name": (
                            None if anchor["app_name"] is None else str(anchor["app_name"])
                        ),
                        "window_title": (
                            None
                            if anchor["window_title"] is None
                            else str(anchor["window_title"])
                        ),
                    },
                    "followers": [
                        {
                            "id": int(r["id"]),
                            "captured_at": str(r["captured_at"]),
                        }
                        for r in follower_rows
                    ],
                    "follower_count": int(anchor["follower_count"]),
                }
            )
    return groupings


async def _read_last_run() -> str | None:
    """Return the kv-stored ISO timestamp of the last manual scan."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_LAST_RUN)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


async def _write_last_run(now_iso: str) -> None:
    """Persist the manual-scan timestamp to ``kv_settings``."""
    async with get_connection() as conn:
        await set_kv(conn, _KV_LAST_RUN, now_iso)


@router.get("/admin/smart-dedup", response_class=HTMLResponse)
async def smart_dedup_page(request: Request) -> HTMLResponse:
    """Render the admin overview — counts, last-run, recent groupings."""
    marked = await _count_marked()
    anchors = await _count_anchors()
    groupings = await _fetch_recent_groupings(_SAMPLE_ANCHORS)
    last_run = await _read_last_run()

    log.info(
        "smart_dedup.page",
        marked=marked,
        anchors=anchors,
        sample_size=len(groupings),
    )

    return templates.TemplateResponse(
        request,
        "smart_dedup.html",
        {
            "title": "Smart dedup",
            "active_nav": "settings",
            "marked": marked,
            "anchors": anchors,
            "groupings": groupings,
            "last_run": last_run,
        },
    )


@router.post("/api/smart-dedup/scan")
async def smart_dedup_scan() -> RedirectResponse:
    """Trigger one detection pass and redirect back to the admin page."""
    result = await detect_trivial_dups()
    now_iso = datetime.now(tz=UTC).isoformat()
    await _write_last_run(now_iso)
    log.info(
        "smart_dedup.manual_scan",
        scanned=result["scanned"],
        marked=result["marked"],
        kept=result["kept"],
    )
    return RedirectResponse(url="/admin/smart-dedup", status_code=303)


@router.get("/api/smart-dedup/stats.json")
async def smart_dedup_stats_json() -> JSONResponse:
    """Return the same counts as the admin page in machine-readable form."""
    marked = await _count_marked()
    anchors = await _count_anchors()
    last_run = await _read_last_run()
    return JSONResponse(
        {
            "marked": marked,
            "anchors": anchors,
            "last_run": last_run,
        }
    )


__all__ = [
    "router",
    "smart_dedup_page",
    "smart_dedup_scan",
    "smart_dedup_stats_json",
]
