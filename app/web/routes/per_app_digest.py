"""Per-app day digest endpoints — `/digest/apps?day=YYYY-MM-DD`.

The HTML page lists every app the user touched on the selected day and
shows the cached one-sentence LLM digest for each one (with a regenerate
button per row). The JSON endpoint feeds the same data to the page so the
table can refresh client-side without a full reload after a regenerate.

Mirrors the design of :mod:`app.web.routes.day_tldr` (v0.36) — the LLM
call is never on the synchronous render path; the HTML ships with cached
rows and the rest are populated by the JSON endpoint on demand.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import TypedDict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.llm.per_app_digest import Status, summarise_app_day
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso
from app.web.templates_engine import templates

router = APIRouter(tags=["per-app-digest"])
log = get_logger("persona.per_app_digest")


class _AppRow(TypedDict):
    app_name: str
    count: int
    tldr: str
    status: Status


class _DigestPayload(TypedDict):
    day: str
    apps: list[_AppRow]


def _validate_day(day: str) -> str:
    """Reject anything that isn't YYYY-MM-DD. Returns the canonical form."""
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="day must be in YYYY-MM-DD form",
        ) from exc
    return parsed.isoformat()


async def _list_apps_for_day(day_iso: str) -> list[tuple[str, int]]:
    """Return (app_name, capture_count) for every app touched on ``day_iso``.

    Sorted by capture count descending so the table shows the heaviest
    apps first — which is also the order the user is most likely to want
    a digest for.
    """
    parsed = datetime.strptime(day_iso, "%Y-%m-%d").date()
    since = datetime.combine(parsed, time.min, tzinfo=UTC)
    until = since + timedelta(days=1)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? "
            "AND app_name IS NOT NULL AND length(app_name) > 0 "
            "GROUP BY app_name ORDER BY n DESC",
            (iso(since), iso(until)),
        )
        rows = await cursor.fetchall()
    return [(str(row["app_name"]), int(row["n"])) for row in rows]


async def _read_cached_tldrs(day_iso: str) -> dict[str, str]:
    """Return ``{app_name: tldr}`` for every cached row on ``day_iso``."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, tldr FROM app_day_digest WHERE day = ?",
            (day_iso,),
        )
        rows = await cursor.fetchall()
    return {str(row["app_name"]): str(row["tldr"]) for row in rows}


async def _collect_rows(day_iso: str) -> list[_AppRow]:
    """Build the per-app row list with cached TL;DRs filled in (no LLM call)."""
    apps = await _list_apps_for_day(day_iso)
    cached = await _read_cached_tldrs(day_iso)
    out: list[_AppRow] = []
    for name, count in apps:
        if name in cached:
            out.append(
                {
                    "app_name": name,
                    "count": count,
                    "tldr": cached[name],
                    "status": "ok",
                }
            )
        else:
            out.append(
                {
                    "app_name": name,
                    "count": count,
                    "tldr": "",
                    "status": "empty",
                }
            )
    return out


@router.get("/digest/apps", response_class=HTMLResponse)
async def per_app_digest_page(
    request: Request,
    day: str = Query(
        default_factory=lambda: datetime.now(UTC).date().isoformat(),
        description="Day in YYYY-MM-DD form",
    ),
) -> HTMLResponse:
    """Render the per-app digest page for ``day`` (defaults to today, UTC)."""
    canonical = _validate_day(day)
    rows = await _collect_rows(canonical)
    log.info(
        "per_app_digest.page.render",
        day=canonical,
        apps=len(rows),
        cached=sum(1 for r in rows if r["status"] == "ok"),
    )
    return templates.TemplateResponse(
        request,
        "per_app_digest.html",
        {
            "title": "Per-app digest",
            "active_nav": "digest",
            "day": canonical,
            "apps": rows,
        },
    )


@router.get("/api/per-app-digest.json", response_class=JSONResponse)
async def per_app_digest_json(
    day: str = Query(..., description="Day in YYYY-MM-DD form"),
    app_name: str | None = Query(
        default=None,
        description=(
            "Optional app to generate lazily. When omitted, returns only the "
            "rows that are already cached (read-only, no LLM call)."
        ),
    ),
) -> JSONResponse:
    """Return the per-app digest payload for ``day``.

    When ``app_name`` is supplied, the matching row is generated on demand
    (cache hit short-circuits) and the response reflects the fresh status.
    When omitted, the response is a pure read of cached state — useful for
    the page to poll for completion without re-triggering LLM calls on
    rows the user has not requested.
    """
    canonical = _validate_day(day)
    rows = await _collect_rows(canonical)
    if app_name is not None:
        target = app_name.strip()
        if not target:
            raise HTTPException(
                status_code=400,
                detail="app_name must be a non-empty string when supplied",
            )
        result = await summarise_app_day(canonical, target)
        for row in rows:
            if row["app_name"] == target:
                row["tldr"] = result["tldr"]
                row["status"] = result["status"]
                break
    payload: _DigestPayload = {"day": canonical, "apps": rows}
    return JSONResponse(payload)


@router.post(
    "/api/per-app-digest/{day}/{app_name}/regenerate",
    response_class=JSONResponse,
)
async def regenerate_per_app_digest(day: str, app_name: str) -> JSONResponse:
    """Force a fresh LLM call for ``(day, app_name)``, overwriting any cache."""
    canonical = _validate_day(day)
    target = app_name.strip()
    if not target:
        raise HTTPException(
            status_code=400,
            detail="app_name must be a non-empty string",
        )
    result = await summarise_app_day(canonical, target, force=True)
    if result["status"] == "missing_config":
        raise HTTPException(
            status_code=400,
            detail=(
                "LLM not configured. Set PERSONA_BYO_API_PROVIDER and "
                "PERSONA_BYO_API_KEY in .env to enable AI features."
            ),
        )
    return JSONResponse(
        {
            "day": canonical,
            "app_name": target,
            "tldr": result["tldr"],
            "status": result["status"],
        }
    )
