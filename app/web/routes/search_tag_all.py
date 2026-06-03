"""Bulk-tag every screenshot that matches the current /search filter set.

v0.88 wires the *Tag all results* button on the search page to a JSON
endpoint that re-runs the same FTS5 + facet pipeline as
:mod:`app.web.routes.search` and then applies a tag to every match
(capped at 1000 rows to keep one request bounded).

The endpoint accepts the same filter inputs as ``GET /search`` —
``q``, ``app``, ``date_from``/``date_to`` (or legacy ``since``/``until``),
``tier``, repeatable ``tag`` (facets only — *not* the tag we're about to
apply), and ``min_w``/``min_h``. The new tag name is the only extra
field. Filters are validated identically to the search route so the
"matching subset" the user sees on screen lines up byte-for-byte with
what gets tagged.

Reuses :func:`app.bulk_tag.bulk_tag` semantics — idempotent tag-row
creation and ``INSERT OR IGNORE`` link insertion via the storage helpers
— but drives the *which screenshots match?* step from the search route's
filter contract instead of re-running ``bulk_tag``'s own FTS5 search,
because ``bulk_tag`` does not know about the post-filter facets
(``app``/``date``/``tier``/``tag``/size).

Audit-logged under ``search.tag_all.apply``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Form, HTTPException, Query
from fastapi.responses import JSONResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.search import search as run_search
from app.storage.db import get_connection
from app.storage.tags import create_tag, tag_screenshot

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.search.tag_all")

router = APIRouter(tags=["search"])

# Cap how many screenshots one call may tag. The /search UI itself
# renders at most 100 hits, but the user's filter set may legitimately
# match more; 1000 is the task's documented ceiling.
_LIMIT_MAX = 1000

# Mirror the validation bounds /search uses for the free-text ``q`` so
# the two endpoints accept exactly the same input shape.
_QUERY_MIN, _QUERY_MAX = 1, 500
_TAG_MIN, _TAG_MAX = 1, 60


def _validate_query(query: str) -> str:
    cleaned = (query or "").strip()
    if not (_QUERY_MIN <= len(cleaned) <= _QUERY_MAX):
        msg = f"q must be {_QUERY_MIN}..{_QUERY_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_tag(tag: str) -> str:
    cleaned = (tag or "").strip()
    if not (_TAG_MIN <= len(cleaned) <= _TAG_MAX):
        msg = f"tag_name must be {_TAG_MIN}..{_TAG_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    # Match the lower-cased normalisation that :func:`app.bulk_tag.bulk_tag`
    # applies so /search/tag-all and the CLI write the same tag row.
    return cleaned.lower()


def _parse_iso_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalise_date(value: str | None) -> str | None:
    """Accept ``YYYY-MM-DD`` (or full ISO) and return the date part."""
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        return None


def _normalise_tag_list(raw: list[str] | None) -> list[str]:
    """Strip / dedupe / order-preserve a repeatable ``?tag=`` list."""
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


async def _filter_ids(
    conn: aiosqlite.Connection,
    ids: list[int],
    *,
    tier: str | None,
    tags: list[str],
    app_name: str | None,
    date_from: str | None,
    date_to: str | None,
    min_w: int | None,
    min_h: int | None,
) -> set[int]:
    """Narrow ``ids`` by the same facets the /search route applies.

    Every condition uses ``?`` bind parameters; no user input is
    interpolated into SQL. AND semantics across facets matches the
    search UI: a row must satisfy every active filter to survive.
    """
    keep_ids: set[int] = set(ids)
    if not ids:
        return keep_ids

    placeholders = ",".join("?" * len(ids))

    if tier:
        cursor = await conn.execute(
            f"SELECT id FROM screenshots WHERE id IN ({placeholders}) AND tier = ?",  # noqa: S608
            (*ids, tier),
        )
        rows = await cursor.fetchall()
        keep_ids &= {int(row["id"]) for row in rows}

    if app_name:
        cursor = await conn.execute(
            f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
            "AND app_name = ?",
            (*ids, app_name),
        )
        rows = await cursor.fetchall()
        keep_ids &= {int(row["id"]) for row in rows}

    if date_from:
        cursor = await conn.execute(
            f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
            "AND DATE(captured_at) >= DATE(?)",
            (*ids, date_from),
        )
        rows = await cursor.fetchall()
        keep_ids &= {int(row["id"]) for row in rows}

    if date_to:
        cursor = await conn.execute(
            f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
            "AND DATE(captured_at) <= DATE(?)",
            (*ids, date_to),
        )
        rows = await cursor.fetchall()
        keep_ids &= {int(row["id"]) for row in rows}

    if min_w is not None:
        cursor = await conn.execute(
            f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
            "AND width IS NOT NULL AND width >= ?",
            (*ids, min_w),
        )
        rows = await cursor.fetchall()
        keep_ids &= {int(row["id"]) for row in rows}

    if min_h is not None:
        cursor = await conn.execute(
            f"SELECT id FROM screenshots WHERE id IN ({placeholders}) "  # noqa: S608
            "AND height IS NOT NULL AND height >= ?",
            (*ids, min_h),
        )
        rows = await cursor.fetchall()
        keep_ids &= {int(row["id"]) for row in rows}

    for tag_name in tags:
        cursor = await conn.execute(
            "SELECT st.screenshot_id FROM screenshot_tags st "  # noqa: S608
            "JOIN tags t ON t.id = st.tag_id "
            f"WHERE t.name = ? AND st.screenshot_id IN ({placeholders})",
            (tag_name, *ids),
        )
        rows = await cursor.fetchall()
        keep_ids &= {int(row["screenshot_id"]) for row in rows}
        if not keep_ids:
            break

    return keep_ids


@router.post("/api/search/tag-all")
async def tag_all_results(
    tag_name: Annotated[str, Form(...)],
    q: Annotated[str, Form(...)],
    app_name: Annotated[str | None, Form(alias="app")] = None,
    since: Annotated[str | None, Form()] = None,
    until: Annotated[str | None, Form()] = None,
    date_from: Annotated[str | None, Form()] = None,
    date_to: Annotated[str | None, Form()] = None,
    tier: Annotated[str | None, Form()] = None,
    tag: Annotated[list[str] | None, Form()] = None,
    min_w: Annotated[int | None, Query(ge=1)] = None,
    min_h: Annotated[int | None, Query(ge=1)] = None,
) -> JSONResponse:
    """Apply ``tag_name`` to every screenshot matching the search filters.

    Returns ``{"matched": <int>, "tagged": <int>, "tag": <name>}``. The
    ``matched`` count is the size of the filtered subset (capped at
    1000); ``tagged`` is how many rows ended up with the link, which
    equals ``matched`` when none of them carried the tag already.
    """
    query_v = _validate_query(q)
    tag_v = _validate_tag(tag_name)
    tier_v = (tier or "").strip() or None
    app_v = (app_name or "").strip() or None
    facet_tags = _normalise_tag_list(tag)
    date_from_v = _normalise_date(date_from)
    date_to_v = _normalise_date(date_to)
    since_dt = _parse_iso_or_none(since)
    until_dt = _parse_iso_or_none(until)

    async with get_connection() as conn:
        # Same FTS5 entry point /search uses. We pass ``limit=_LIMIT_MAX``
        # so the eventual tagged set can grow up to the documented cap;
        # the merged search UI itself only renders 100 hits.
        hits = await run_search(
            conn,
            query=query_v,
            limit=_LIMIT_MAX,
            since=since_dt,
            until=until_dt,
            app_name=app_v,
        )
        ids = [int(hit.screenshot_id) for hit in hits]

        kept = await _filter_ids(
            conn,
            ids,
            tier=tier_v,
            tags=facet_tags,
            app_name=app_v,
            date_from=date_from_v,
            date_to=date_to_v,
            min_w=min_w,
            min_h=min_h,
        )
        # Preserve hit ordering so the "first N tagged" outcome is
        # deterministic when the FTS5 result list happens to be sorted.
        ordered_ids = [sid for sid in ids if sid in kept][:_LIMIT_MAX]
        matched = len(ordered_ids)

        tagged = 0
        if matched:
            tag_id = await create_tag(conn, name=tag_v)
            for sid in ordered_ids:
                await tag_screenshot(conn, sid, tag_id)
                tagged += 1

    log.info(
        "search.tag_all.applied",
        tag=tag_v,
        query=query_v,
        app=app_v,
        tier=tier_v,
        facet_tags=facet_tags,
        date_from=date_from_v,
        date_to=date_to_v,
        min_w=min_w,
        min_h=min_h,
        matched=matched,
        tagged=tagged,
    )
    await log_action(
        "search.tag_all.apply",
        target=tag_v,
        detail=(
            f"q={query_v} app={app_v or '-'} tier={tier_v or '-'} "
            f"date_from={date_from_v or '-'} date_to={date_to_v or '-'} "
            f"facet_tags={','.join(facet_tags) or '-'} "
            f"min_w={min_w if min_w is not None else '-'} "
            f"min_h={min_h if min_h is not None else '-'} "
            f"matched={matched} tagged={tagged}"
        ),
    )

    return JSONResponse(
        {
            "matched": matched,
            "tagged": tagged,
            "tag": tag_v,
        }
    )


__all__ = ["router"]
