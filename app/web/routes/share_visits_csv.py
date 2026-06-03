"""HTTP route for the v0.61 share-visits CSV export.

``GET /export/share-visits.csv?days=90`` streams a ``text/csv`` document
with one row per recorded hit against the v0.55 ``share_visit`` table:
the public viewer behind ``/shot/share/{shot_id}/{token}`` writes one
row there every time a recipient opens a signed link.

Columns (in order):
    shot_id     — integer FK back to ``screenshots.id`` (no DB-level FK).
    visited_at  — UTC timestamp the row was written, ISO-8601 style as
                  stored by SQLite (``YYYY-MM-DD HH:MM:SS``).
    ua          — User-Agent truncated to 200 chars (may be empty).
    ip_prefix   — Coarse first-two-segments of the client IP (may be
                  empty); never a full address — see migration 055.

The endpoint streams via :class:`fastapi.responses.StreamingResponse` to
match the other ``/export/*.csv`` routes in the codebase (see
:mod:`app.web.routes.stats_csv`). For the share-visits volumes Persona
deals with (a few hundred rows even on busy days) we materialise the
whole CSV into a single in-memory chunk — keeps the route trivially
testable and the ``Content-Length`` header authoritative for clients
that want a progress bar.

The ``days`` filter is applied at the SQL layer with a parametrised
``datetime('now', '-N days')`` so the comparison happens against the
server's clock rather than Python's, avoiding off-by-one drift around
midnight.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.share_visits.csv")

router = APIRouter(prefix="/export", tags=["share-visits-csv"])

# Mirror the clamp window we use for the other ``/export/*.csv`` routes
# (see :mod:`app.web.routes.stats_csv`). 10 years is well past any
# realistic audit horizon while keeping the integer small enough that
# the SQLite ``datetime('now', '-N days')`` modifier behaves.
_MIN_DAYS = 1
_MAX_DAYS = 3650

_CSV_COLUMNS: tuple[str, ...] = ("shot_id", "visited_at", "ua", "ip_prefix")


@router.get("/share-visits.csv", response_model=None)
async def export_share_visits_csv(
    days: int = Query(default=90, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> StreamingResponse:
    """Stream every ``share_visit`` row from the last ``days`` days."""
    try:
        body = await _render_share_visits_csv(days=days)
    except Exception:
        log.exception("share_visits_csv.route.failed", days=days)
        raise HTTPException(
            status_code=500,
            detail="share-visits CSV export failed",
        ) from None

    payload = body.encode("utf-8")
    filename = f"persona-share-visits-{days}d.csv"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info("share_visits_csv.route.ok", days=days, bytes=len(payload))

    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


async def _render_share_visits_csv(*, days: int) -> str:
    """Read the ``share_visit`` table and return the CSV body as a string.

    Split out from the route so the CLI subcommand in :mod:`app.cli` can
    reuse the exact same query + serialisation without going through
    FastAPI. The function is also the natural unit-test seam.

    Parametrised SQL only — the ``days`` integer flows in as a bound
    parameter through SQLite's ``datetime('now', ?)`` modifier so we
    never interpolate it into the query string.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)

    rows_written = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT shot_id, visited_at, ua, ip_prefix
            FROM share_visit
            WHERE visited_at >= datetime('now', ?)
            ORDER BY visited_at ASC, id ASC
            """,
            (f"-{days} days",),
        )
        async for row in cursor:
            writer.writerow(
                (
                    int(row["shot_id"]),
                    str(row["visited_at"]),
                    row["ua"] if row["ua"] is not None else "",
                    row["ip_prefix"] if row["ip_prefix"] is not None else "",
                )
            )
            rows_written += 1

    log.info(
        "share_visits_csv.render.ok",
        days=days,
        rows=rows_written,
    )
    return buffer.getvalue()


__all__ = ["router"]
