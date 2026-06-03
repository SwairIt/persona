"""Search route — FTS5 keyword + optional semantic ranking.

v0.41 adds rich filter facets: ``app`` (string), ``date_from``/``date_to``
(``YYYY-MM-DD``) and ``tag`` (repeatable). v0.52 adds ``min_w`` /
``min_h`` (integer pixels) backed by :mod:`app.shot_dimensions`. When any
of these are set we post-filter the merged hit list against the SQLite
catalogue using bind parameters only — no string interpolation of user
input ever reaches SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.embeddings import EmbeddingsNotAvailable, is_available, semantic_search
from app.embeddings.model import load_model  # noqa: F401 (eager-import sanity)
from app.logging_setup import get_logger
from app.search import search as run_search
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.search_history import clear_history, list_recent, record_query
from app.web.templates_engine import templates

log = get_logger("persona.search.facets")
log_sort = get_logger("persona.grid_sort")

router = APIRouter(tags=["search"])

# Whitelist of allowed ``sort_by`` query values. Hits come back from the
# merge step as in-memory dicts, so we sort in Python — no SQL involved,
# which means the whitelist is the only guard we need against bogus
# values (e.g., URL fuzzing).
_SORT_OPTIONS: tuple[str, ...] = (
    "captured_at",
    "captured_at_asc",
    "app_name",
    "ocr_length",
)
_DEFAULT_SORT = "captured_at"


def _coerce_sort(value: str | None) -> str:
    """Reduce arbitrary user input to a whitelisted sort key."""
    if value and value in _SORT_OPTIONS:
        return value
    return _DEFAULT_SORT


def _sort_hits(hits: list[dict[str, Any]], sort_key: str) -> list[dict[str, Any]]:
    """Re-order merged search hits by a whitelisted field."""
    if sort_key == _DEFAULT_SORT:
        return hits

    def captured_key(h: dict[str, Any]) -> str:
        raw = h.get("captured_at")
        return str(raw) if raw is not None else ""

    if sort_key == "captured_at_asc":
        return sorted(hits, key=captured_key)
    if sort_key == "app_name":
        return sorted(
            hits,
            key=lambda h: (
                # NULL/missing app_name sinks to the bottom of an ASC sort.
                h.get("app_name") is None,
                str(h.get("app_name") or ""),
                captured_key(h),
            ),
        )
    if sort_key == "ocr_length":
        # ``snippet`` is the only OCR-derived text we keep on the hit
        # dict; fall back to 0 when it's missing. Sort longest-first.
        return sorted(
            hits,
            key=lambda h: (len(str(h.get("snippet") or "")), captured_key(h)),
            reverse=True,
        )
    return hits


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query(default=""),
    app_name: str | None = Query(default=None, alias="app"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    mode: str = Query(default="hybrid"),
    tier: str | None = Query(default=None),
    tag: Annotated[list[str] | None, Query()] = None,
    min_w: int | None = Query(default=None, ge=1),
    min_h: int | None = Query(default=None, ge=1),
    sort_by: str = Query(default=_DEFAULT_SORT),
) -> HTMLResponse:
    """Render search page with optional tier / tag / app / date / size post-filters."""
    since_dt = _parse_iso_or_none(since)
    until_dt = _parse_iso_or_none(until)
    sort_key = _coerce_sort(sort_by)

    # Normalise repeatable ``?tag=`` query params: drop blanks, dedupe,
    # preserve order so the template can faithfully echo what the user
    # submitted. The legacy single-value ``tag`` form field still works
    # because FastAPI happily promotes a single value to a one-item list.
    tags: list[str] = _normalise_tags(tag)
    # Back-compat: existing templates / links read ``tag`` as a scalar.
    tag_single: str | None = tags[0] if tags else None

    # Date facets accept either ``YYYY-MM-DD`` (v0.41 facet form) or a
    # full ISO timestamp (legacy ``since``/``until``). We keep them as
    # raw strings here and let the filter step validate the shape.
    date_from_norm = _normalise_date(date_from)
    date_to_norm = _normalise_date(date_to)

    settings = get_settings()
    chosen_mode = _coerce_mode(mode, settings.embeddings_enabled and is_available())

    hits: list[Any] = []
    semantic_unavailable: str | None = None
    recent_searches: list[dict[str, Any]] = []

    async with get_connection() as conn:
        if q:
            if chosen_mode in {"hybrid", "fts"}:
                fts_hits = await run_search(
                    conn,
                    query=q,
                    limit=100,
                    since=since_dt,
                    until=until_dt,
                    app_name=app_name,
                )
            else:
                fts_hits = []

            sem_hits: list[Any] = []
            if chosen_mode in {"hybrid", "semantic"}:
                try:
                    sem_hits = await semantic_search(
                        conn,
                        query=q,
                        limit=100,
                        since=since_dt,
                        until=until_dt,
                        app_name=app_name,
                    )
                except EmbeddingsNotAvailable as exc:
                    semantic_unavailable = str(exc)
                    sem_hits = []

            await record_query(conn, query=q, mode=chosen_mode)
            hits = _merge_results(fts_hits, sem_hits, mode=chosen_mode)

        recent_searches = await list_recent(conn)

    if q and (tier or tags or app_name or date_from_norm or date_to_norm or min_w or min_h):
        hits = await _apply_post_filters(
            hits,
            tier=tier,
            tags=tags,
            app_name=app_name,
            date_from=date_from_norm,
            date_to=date_to_norm,
            min_w=min_w,
            min_h=min_h,
        )
        log.debug(
            "search.facets.applied",
            query=q,
            tier=tier,
            tags=tags,
            app_name=app_name,
            date_from=date_from_norm,
            date_to=date_to_norm,
            min_w=min_w,
            min_h=min_h,
            remaining=len(hits),
        )

    if hits and sort_key != _DEFAULT_SORT:
        hits = _sort_hits(hits, sort_key)
        log_sort.info("grid_sort.search", sort_by=sort_key, count=len(hits))

    is_htmx = request.headers.get("HX-Request") == "true"
    template_name = "_search_results.html" if is_htmx else "search.html"

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "title": f"Search: {q}" if q else "Search",
            "active_nav": "search",
            "query": q,
            "app_name": app_name,
            "since": since,
            "until": until,
            "date_from": date_from_norm,
            "date_to": date_to_norm,
            "mode": chosen_mode,
            "tier": tier,
            "tag": tag_single,
            "tags": tags,
            "min_w": min_w,
            "min_h": min_h,
            "sort_by": sort_key,
            "sort_options": _SORT_OPTIONS,
            "hits": hits,
            "total": len(hits),
            "embeddings_enabled": settings.embeddings_enabled,
            "embeddings_available": is_available(),
            "semantic_unavailable": semantic_unavailable,
            "recent_searches": recent_searches,
        },
    )


@router.get("/api/search-history")
async def api_search_history() -> JSONResponse:
    """Return the recent search history as JSON."""
    async with get_connection() as conn:
        recent = await list_recent(conn)
    return JSONResponse({"recent": recent})


@router.post("/api/search-history/clear")
async def api_search_history_clear() -> JSONResponse:
    """Wipe search history; return how many rows were deleted."""
    async with get_connection() as conn:
        deleted = await clear_history(conn)
    return JSONResponse({"deleted": deleted})


def _coerce_mode(value: str, semantic_ready: bool) -> str:
    mode = value.lower().strip()
    if mode not in {"fts", "semantic", "hybrid"}:
        mode = "hybrid"
    if mode in {"semantic", "hybrid"} and not semantic_ready:
        return "fts"
    return mode


def _merge_results(fts_hits: list[Any], sem_hits: list[Any], *, mode: str) -> list[dict[str, Any]]:
    """Merge keyword + semantic results into a single ranked list."""
    out: dict[int, dict[str, Any]] = {}

    for rank, h in enumerate(fts_hits, start=1):
        out[h.screenshot_id] = {
            "screenshot_id": h.screenshot_id,
            "captured_at": h.captured_at,
            "thumbnail_path": h.thumbnail_path,
            "app_name": h.app_name,
            "window_title": h.window_title,
            "snippet": h.snippet,
            "fts_rank": rank,
            "similarity": None,
            "score": 1.0 / rank,
        }

    for sem in sem_hits:
        sid = sem["screenshot_id"]
        if sid in out:
            out[sid]["similarity"] = sem["similarity"]
            out[sid]["score"] = out[sid]["score"] + sem["similarity"]
        else:
            out[sid] = {
                "screenshot_id": sid,
                "captured_at": sem["captured_at"],
                "thumbnail_path": sem["thumbnail_path"],
                "app_name": sem["app_name"],
                "window_title": sem["window_title"],
                "snippet": sem["snippet"],
                "fts_rank": None,
                "similarity": sem["similarity"],
                "score": sem["similarity"],
            }

    merged = sorted(out.values(), key=lambda h: h["score"], reverse=True)
    if mode == "semantic":
        merged = [h for h in merged if h["similarity"] is not None]
    return merged[:100]


async def _apply_post_filters(
    hits: list[dict[str, Any]],
    *,
    tier: str | None,
    tags: list[str],
    app_name: str | None,
    date_from: str | None,
    date_to: str | None,
    min_w: int | None = None,
    min_h: int | None = None,
) -> list[dict[str, Any]]:
    """Filter merged hits by tier / tags / app / date / size via DB lookups.

    All SQL uses ``?`` bind parameters; no user input is interpolated.
    Each filter independently narrows ``keep_ids`` so a hit must satisfy
    *every* active facet to survive (AND semantics, like the form UI).

    ``min_w`` / ``min_h`` filter on the pixel dimensions written by
    :mod:`app.shot_dimensions` (v0.52). Rows with ``NULL`` width or
    height — legacy captures the backfill hasn't visited yet — drop out
    of the result set, mirroring the SQL ``>= ?`` comparison semantics
    against an unknown left-hand side.
    """
    if not hits:
        return hits
    ids = [int(h["screenshot_id"]) for h in hits]
    keep_ids: set[int] = set(ids)

    async with get_connection() as conn:
        if tier:
            placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                f"SELECT id FROM screenshots WHERE id IN ({placeholders}) AND tier = ?",  # noqa: S608
                (*ids, tier),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["id"]) for row in rows}

        if app_name:
            placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
                "AND app_name = ?",
                (*ids, app_name),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["id"]) for row in rows}

        if date_from:
            placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
                "AND DATE(captured_at) >= DATE(?)",
                (*ids, date_from),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["id"]) for row in rows}

        if date_to:
            placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
                "AND DATE(captured_at) <= DATE(?)",
                (*ids, date_to),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["id"]) for row in rows}

        if min_w is not None:
            placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
                "AND width IS NOT NULL AND width >= ?",
                (*ids, min_w),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["id"]) for row in rows}

        if min_h is not None:
            placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
                "AND height IS NOT NULL AND height >= ?",
                (*ids, min_h),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["id"]) for row in rows}

        # Tag facet — AND across tags: a hit must carry *every* requested
        # tag, matching how stacked checkboxes feel intuitively.
        for tag_name in tags:
            id_placeholders = ",".join("?" * len(ids))
            cursor = await conn.execute(
                "SELECT st.screenshot_id FROM screenshot_tags st "  # noqa: S608
                "JOIN tags t ON t.id = st.tag_id "
                f"WHERE t.name = ? AND st.screenshot_id IN ({id_placeholders})",
                (tag_name, *ids),
            )
            rows = await cursor.fetchall()
            keep_ids &= {int(row["screenshot_id"]) for row in rows}
            if not keep_ids:
                break

    return [h for h in hits if int(h["screenshot_id"]) in keep_ids]


def _parse_iso_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalise_tags(raw: list[str] | None) -> list[str]:
    """Strip / dedupe / order-preserve a ``?tag=`` multi-value list."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _normalise_date(value: str | None) -> str | None:
    """Accept ``YYYY-MM-DD`` (or longer ISO) and return the date part.

    Anything that doesn't parse is dropped silently — the form input is
    ``<input type="date">`` in modern browsers, so garbage is rare and a
    permissive parser keeps URL-driven traffic working.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        # Bare ``YYYY-MM-DD`` already comes back from fromisoformat, so
        # if we land here it's truly malformed — drop it.
        return None
