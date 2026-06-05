"""Smart-pin suggestions dashboard + accept/dismiss API (v1.50).

Renders pending picks produced by :mod:`app.llm.smart_pin` /
:mod:`app.workers.smart_pin_worker` and lets the operator either:

* **Accept** — stamps ``accepted_at`` and pins the underlying
  screenshot (mirrors the existing manual-pin endpoint logic).
* **Dismiss** — stamps ``dismissed_at`` so the row drops off the
  pending list without touching the screenshots table.

Routes:

* ``GET /memory/smart-pins`` — HTML dashboard.
* ``POST /api/smart-pin/{suggestion_id}/accept`` — JSON.
* ``POST /api/smart-pin/{suggestion_id}/dismiss`` — JSON.
* ``GET /api/smart-pin/pending.json`` — machine-readable list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.outbox import dispatch_event as outbox_dispatch
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.tiers import pin_screenshot
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.web.smart_pin")

router = APIRouter(tags=["smart-pin"])


@router.get("/memory/smart-pins", response_class=HTMLResponse)
async def smart_pin_dashboard(request: Request) -> HTMLResponse:
    """Render the pending smart-pin suggestions list."""
    suggestions = await _list_pending()
    return templates.TemplateResponse(
        request,
        "smart_pin_suggestions.html",
        {
            "title": "Умные пины",
            "active_nav": "memory",
            "suggestions": suggestions,
        },
    )


@router.get("/api/smart-pin/pending.json", response_class=JSONResponse)
async def smart_pin_pending_json() -> JSONResponse:
    """Return the same pending list as the HTML page, as JSON."""
    suggestions = await _list_pending()
    return JSONResponse({"count": len(suggestions), "items": suggestions})


@router.post(
    "/api/smart-pin/{suggestion_id}/accept",
    response_class=JSONResponse,
)
async def smart_pin_accept(suggestion_id: int) -> JSONResponse:
    """Accept a suggestion — stamp ``accepted_at`` and pin the shot.

    The accept is a one-shot operation: a row that is already
    accepted or already dismissed cannot be accepted again. The
    pin itself goes through :func:`app.storage.tiers.pin_screenshot`
    so the existing tier-sweep guarantees apply.
    """
    async with get_connection() as conn:
        suggestion = await _load_suggestion(conn, suggestion_id)
        if suggestion is None:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        if suggestion["accepted_at"] is not None:
            raise HTTPException(status_code=409, detail="Already accepted")
        if suggestion["dismissed_at"] is not None:
            raise HTTPException(status_code=409, detail="Already dismissed")

        screenshot_id = int(suggestion["screenshot_id"])
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            # FK is ON DELETE CASCADE, but a manual delete inside a
            # transaction window could still leave a dangling row briefly.
            raise HTTPException(status_code=404, detail="Screenshot not found")

        await pin_screenshot(conn, screenshot_id)
        await conn.execute(
            "UPDATE smart_pin_suggestion "
            "SET accepted_at = datetime('now') "
            "WHERE id = ?",
            (suggestion_id,),
        )
        await conn.commit()

    await outbox_dispatch(
        "shot_pinned",
        {
            "shot_id": screenshot_id,
            "captured_at": shot.captured_at.isoformat(),
            "app": shot.app_name or "",
            "source": "smart_pin_suggestion",
        },
    )
    log.info(
        "smart_pin.accept",
        suggestion_id=suggestion_id,
        screenshot_id=screenshot_id,
    )
    return JSONResponse(
        {
            "suggestion_id": suggestion_id,
            "screenshot_id": screenshot_id,
            "status": "accepted",
        }
    )


@router.post(
    "/api/smart-pin/{suggestion_id}/dismiss",
    response_class=JSONResponse,
)
async def smart_pin_dismiss(suggestion_id: int) -> JSONResponse:
    """Dismiss a suggestion — stamp ``dismissed_at``, leave shot alone."""
    async with get_connection() as conn:
        suggestion = await _load_suggestion(conn, suggestion_id)
        if suggestion is None:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        if suggestion["accepted_at"] is not None:
            raise HTTPException(status_code=409, detail="Already accepted")
        if suggestion["dismissed_at"] is not None:
            raise HTTPException(status_code=409, detail="Already dismissed")

        await conn.execute(
            "UPDATE smart_pin_suggestion "
            "SET dismissed_at = datetime('now') "
            "WHERE id = ?",
            (suggestion_id,),
        )
        await conn.commit()

    log.info("smart_pin.dismiss", suggestion_id=suggestion_id)
    return JSONResponse(
        {"suggestion_id": suggestion_id, "status": "dismissed"}
    )


async def _list_pending() -> list[dict[str, Any]]:
    """Read pending suggestions joined with screenshot context.

    Ordered by ``score DESC`` so the most-confident picks float to the
    top of the review list. ``created_at`` ties are broken by ``id`` so
    the order is stable across reloads.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                sp.id              AS id,
                sp.screenshot_id   AS screenshot_id,
                sp.reason          AS reason,
                sp.score           AS score,
                sp.created_at      AS created_at,
                s.captured_at      AS captured_at,
                COALESCE(s.app_name, '')     AS app_name,
                COALESCE(s.window_title, '') AS window_title,
                COALESCE(s.thumbnail_path, '') AS thumbnail_path
            FROM smart_pin_suggestion AS sp
            JOIN screenshots AS s ON s.id = sp.screenshot_id
            WHERE sp.accepted_at IS NULL
              AND sp.dismissed_at IS NULL
            ORDER BY sp.score DESC, sp.id DESC
            """
        )
        rows = await cursor.fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "screenshot_id": int(row["screenshot_id"]),
                "reason": str(row["reason"] or ""),
                "score": float(row["score"] or 0.0),
                "created_at": str(row["created_at"] or ""),
                "captured_at": str(row["captured_at"] or ""),
                "app_name": str(row["app_name"] or ""),
                "window_title": str(row["window_title"] or ""),
                "thumbnail_path": str(row["thumbnail_path"] or ""),
            }
        )
    return out


async def _load_suggestion(
    conn: aiosqlite.Connection,
    suggestion_id: int,
) -> aiosqlite.Row | None:
    """Read a single suggestion row by primary key."""
    cursor = await conn.execute(
        """
        SELECT id, screenshot_id, accepted_at, dismissed_at
        FROM smart_pin_suggestion
        WHERE id = ?
        """,
        (suggestion_id,),
    )
    return await cursor.fetchone()


__all__ = ["router"]
