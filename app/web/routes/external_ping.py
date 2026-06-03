"""External heartbeat / idle-ping endpoint (v0.74).

External scripts (CLI focus timers, CI watchers, meeting bots, ...) can
POST a tiny JSON heartbeat at ``/api/ping`` to mark intervals during
which the user was active even though Persona's own screenshot loop was
paused or disabled. The resulting rows in ``external_ping`` give the
``/admin/external-pings`` page (and future time-tracking widgets) a
secondary activity signal that survives Persona being off.

Endpoints
---------
* ``POST /api/ping``            — record a heartbeat. JSON body:
                                  ``{"source": str, "label": str?}``.
                                  Returns ``{"id": int, "ts": str}`` 201.
* ``GET  /admin/external-pings`` — most-recent pings as a Tailwind table.

Design notes
------------
The ingest path is deliberately tolerant: ``source`` is stripped + capped
to ``_MAX_SOURCE_LEN`` chars and a missing / whitespace-only value is a
400, but everything else is best-effort. ``label`` is normalised to
``None`` when empty so the admin column reads as a clean em-dash instead
of an empty cell.

We do not authenticate the ping endpoint itself — Persona is a
single-user, localhost-by-default service, and the whole point is that a
random script on the same machine can fire-and-forget without juggling
tokens. If you expose Persona on a LAN, gate ``/api/ping`` at the proxy.

All SQL is parametrised; the admin page is read-only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["external_ping"])
log = get_logger("persona.external_ping")

# Hard caps to keep a single row small and the admin table render-able.
# Sources are short identifiers ("cli-timer", "ci-watch"); 64 chars is
# more than enough but tight enough that a typo'd 10 kB request body
# can't bloat the table.
_MAX_SOURCE_LEN = 64
# Labels are user-supplied context (project / meeting names). 256 chars
# matches what most diary fields in Persona allow without scrolling.
_MAX_LABEL_LEN = 256
# Default page size for /admin/external-pings — dense enough to spot
# patterns, short enough that the page paints instantly on first load.
_RECENT_LIMIT_DEFAULT = 200
_RECENT_LIMIT_MAX = 1000


class PingRequest(BaseModel):
    """JSON body accepted by ``POST /api/ping``.

    ``source`` is required; an empty / whitespace-only value is rejected
    at the normalisation step below rather than via pydantic ``min_length``
    so we can return a friendly 400 with the same message regardless of
    whether the field is missing or whitespace.
    """

    source: str = Field(..., max_length=_MAX_SOURCE_LEN * 4)
    label: str | None = Field(default=None, max_length=_MAX_LABEL_LEN * 4)


def _normalise_source(raw: str) -> str:
    """Strip + length-cap ``source``; reject empty values with a 400.

    The pydantic ``max_length`` above is intentionally generous (4x) so a
    client sending leading whitespace + a long-ish identifier still
    reaches this normaliser; the hard cap is enforced here after strip().
    """
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="source must not be empty")
    if len(text) > _MAX_SOURCE_LEN:
        text = text[:_MAX_SOURCE_LEN]
    return text


def _normalise_label(raw: str | None) -> str | None:
    """Strip + length-cap ``label``; collapse empty to ``None``."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) > _MAX_LABEL_LEN:
        text = text[:_MAX_LABEL_LEN]
    return text


@router.post("/api/ping", response_class=JSONResponse)
async def create_ping(payload: PingRequest) -> JSONResponse:
    """Record a single external heartbeat.

    Returns the new row id and the server-assigned timestamp so the
    caller can correlate its local log with what landed in the DB.
    """
    source = _normalise_source(payload.source)
    label = _normalise_label(payload.label)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO external_ping (source, label) VALUES (?, ?)",
            (source, label),
        )
        row_id = cursor.lastrowid
        if row_id is None:
            msg = "INSERT did not return a row id"
            raise RuntimeError(msg)
        await conn.commit()

        cursor = await conn.execute(
            "SELECT ts FROM external_ping WHERE id = ?",
            (row_id,),
        )
        row = await cursor.fetchone()
    if row is None:  # pragma: no cover — sanity guard, INSERT just succeeded
        msg = f"external_ping #{row_id} vanished immediately after insert"
        raise RuntimeError(msg)

    ts = str(row["ts"])
    log.info(
        "external_ping.recorded",
        ping_id=row_id,
        source=source,
        has_label=label is not None,
    )
    return JSONResponse({"id": int(row_id), "ts": ts}, status_code=201)


@router.get("/admin/external-pings", response_class=HTMLResponse)
async def external_pings_page(
    request: Request,
    limit: int = Query(default=_RECENT_LIMIT_DEFAULT, ge=1, le=_RECENT_LIMIT_MAX),
    source: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the most-recent heartbeats as a Tailwind table.

    ``limit`` lets an operator widen the window for after-the-fact
    debugging without pagination ceremony — capped at
    ``_RECENT_LIMIT_MAX`` to keep the page render bounded. ``source`` is
    an exact-match filter (heartbeat sources are short identifiers, so
    LIKE-substring search would mostly produce false positives).
    """
    filter_source = (source or "").strip() or None

    async with get_connection() as conn:
        if filter_source is not None:
            cursor = await conn.execute(
                "SELECT id, source, label, ts FROM external_ping "
                "WHERE source = ? "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (filter_source, limit),
            )
        else:
            cursor = await conn.execute(
                "SELECT id, source, label, ts FROM external_ping "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()

        count_cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM external_ping"
        )
        total_row = await count_cursor.fetchone()
        total = int(total_row["n"]) if total_row is not None else 0

    items: list[dict[str, Any]] = [
        {
            "id": int(row["id"]),
            "source": str(row["source"]),
            "label": (str(row["label"]) if row["label"] is not None else None),
            "ts": str(row["ts"]),
        }
        for row in rows
    ]
    log.info(
        "external_ping.page",
        item_count=len(items),
        total=total,
        filter_source=filter_source,
        limit=limit,
    )
    return templates.TemplateResponse(
        request,
        "external_pings.html",
        {
            "title": "External pings",
            "active_nav": "settings",
            "items": items,
            "total": total,
            "limit": limit,
            "filter_source": filter_source or "",
        },
    )
