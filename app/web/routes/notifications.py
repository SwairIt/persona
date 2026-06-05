"""HTTP surface for the v1.44 focus-aware notification queue.

Four routes, all under ``/api/notifications``:

* ``GET  /stream``      — long-lived Server-Sent Events channel. Every
                           ~5 s we query the unseen queue and push the
                           batch as a JSON ``data:`` frame. When
                           :func:`app.focus.current_session` reports an
                           active (un-paused) focus session, the loop
                           emits a comment heartbeat instead — so the
                           connection stays warm and reverse proxies do
                           not idle-kill it, but the operator is not
                           interrupted mid-pomodoro.
* ``GET  /unseen.json`` — plain JSON snapshot for non-SSE polling
                           clients (the bell widget uses it for its
                           initial render before opening the stream).
* ``POST /{id}/seen``   — mark one notification as read.
* ``POST /all-seen``    — clear the badge entirely.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — per task spec, ``main.py`` is off-limits.
Wire it up with::

    from app.web.routes import notifications as notif_routes
    app.include_router(notif_routes.router)
"""

from __future__ import annotations

import json
import os
import secrets
from typing import TYPE_CHECKING, Final

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse

from app import focus, notifications
from app.logging_setup import get_logger
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(tags=["notifications"])
"""Single router that owns both the ``/api/notifications/...`` JSON / SSE
surface and the ``/widget/notification-bell`` HTML fragment. The path
prefixes are written out on each decorator rather than via ``prefix=``
so a downstream :func:`fastapi.FastAPI.include_router` call can wire
the whole thing up with one statement."""

log = get_logger("persona.notifications.routes")

POLL_INTERVAL_SECONDS: Final[float] = 5.0
"""How often the SSE loop polls the unseen queue. Five seconds matches
the v0.66 push-notif cadence and keeps the SQLite read negligible."""

_TEST_MAX_EVENTS_ENV: Final[str] = "PERSONA_NOTIF_SSE_TEST_MAX_EVENTS"
"""Test envelope — when set, the stream terminates after this many
frames (data + heartbeat combined) so ``httpx.AsyncClient`` does not
hang on what is otherwise an infinite generator."""


def _focus_active(session: focus.FocusSession | None) -> bool:
    """Return True when a focus session should suppress notifications.

    We treat *any* open session (``ended_at is None``) as active. The
    focus module does not currently expose a "paused" flag on the
    session row — pausing is a client-side timer concern — so an open
    row is taken to mean "the operator is in a session" and we silence
    user-facing emission until they end it.
    """
    return session is not None and session["ended_at"] is None


def _encode_data_frame(payload: object) -> bytes:
    """Encode one ``data: <json>\\n\\n`` SSE frame."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def _encode_heartbeat(reason: str) -> bytes:
    """Encode an SSE comment line — invisible to ``EventSource`` consumers.

    Comments (lines starting with ``:``) keep the TCP connection warm
    without firing a client-side ``onmessage``. We use them during
    focus suppression so the browser doesn't reconnect every few
    seconds (which would re-open the DB read) yet the operator stays
    undisturbed.
    """
    return f": heartbeat {reason}\n\n".encode()


def _resolve_test_limit() -> int | None:
    """Read the optional test cap from the environment."""
    raw = os.getenv(_TEST_MAX_EVENTS_ENV)
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


async def _event_stream(request: Request) -> AsyncIterator[bytes]:
    """Yield SSE frames until the client disconnects.

    Loop body:

    1. Check the request for disconnect — fast exit when the browser
       navigates away.
    2. Read the current focus session. If one is open and un-paused,
       emit a comment heartbeat and skip the unseen query entirely
       (cheap path during a pomodoro).
    3. Otherwise query :func:`app.notifications.list_unseen`. When the
       list is non-empty, push it as one ``data:`` frame. An empty
       result emits a heartbeat so proxies do not idle-kill us.
    4. Sleep :data:`POLL_INTERVAL_SECONDS`.
    """
    limit = _resolve_test_limit()
    emitted = 0

    while True:
        if await request.is_disconnected():
            return

        try:
            current = await focus.current_session()
        except Exception as exc:  # pragma: no cover — defensive
            # A failed focus lookup should not nuke the whole stream;
            # log and treat as "no active focus" so notifications keep
            # flowing.
            log.warning("notifications.focus_lookup_failed", error=str(exc))
            current = None

        if _focus_active(current):
            frame = _encode_heartbeat("focus")
            log.debug("notifications.suppressed_by_focus")
        else:
            try:
                batch = await notifications.list_unseen()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("notifications.list_unseen_failed", error=str(exc))
                batch = []
            if batch:
                frame = _encode_data_frame({"type": "notifications", "items": batch})
            else:
                frame = _encode_heartbeat("idle")

        yield frame
        emitted += 1
        if limit is not None and emitted >= limit:
            return

        # ``anyio.sleep`` is the cooperative checkpoint — it lets the
        # ASGI server propagate disconnects and other tasks run.
        await anyio.sleep(POLL_INTERVAL_SECONDS)


@router.get("/api/notifications/stream")
async def stream(request: Request) -> StreamingResponse:
    """Long-lived SSE stream of unseen notifications (focus-aware)."""
    log.debug("notifications.sse.connect")
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _event_stream(request),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/api/notifications/unseen.json")
async def unseen_json() -> JSONResponse:
    """Return the current unseen queue as a JSON list."""
    items = await notifications.list_unseen()
    return JSONResponse({"items": items, "count": len(items)})


@router.post("/api/notifications/{notif_id}/seen")
async def mark_one_seen(notif_id: int) -> JSONResponse:
    """Mark a single notification as read."""
    await notifications.mark_seen(notif_id)
    return JSONResponse({"status": "ok", "id": notif_id})


@router.post("/api/notifications/all-seen")
async def clear_badge() -> JSONResponse:
    """Mark every unseen notification as read; clears the bell badge."""
    cleared = await notifications.mark_all_seen()
    return JSONResponse({"status": "ok", "cleared": cleared})


@router.get("/widget/notification-bell", response_class=HTMLResponse)
async def notification_bell_widget(request: Request) -> HTMLResponse:
    """Render the bell + badge + collapsible list HTML fragment.

    Designed to be embedded into any host page via HTMX rather than
    mutating ``base.html``::

        <div hx-get="/widget/notification-bell" hx-trigger="load"></div>

    A short random ``widget_id`` is injected into the template so
    multiple bells on one page (the dashboard might want one in the
    header *and* one in a sidebar tile) do not collide on element IDs.
    """
    widget_id = secrets.token_hex(4)
    log.info("notifications.widget.render", widget_id=widget_id)
    return templates.TemplateResponse(
        request,
        "_notification_bell.html",
        {"widget_id": widget_id},
    )


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "router",
]
