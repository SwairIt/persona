"""Live status pill — Server-Sent Events stream.

The header status pill (see ``base.html``) historically polled three JSON
endpoints every five seconds. v0.40 replaces that polling loop with a
single long-lived SSE connection that:

* emits a ``status`` event every two seconds with the current capture /
  OCR snapshot;
* emits a ``heartbeat`` event whenever a background worker calls
  :func:`publish_heartbeat` (driven from
  ``app.workers.heartbeat.beat``).

The route hand-rolls the SSE wire format (``data: <json>\\n\\n``) so we
keep our dependency surface flat. If ``sse_starlette`` is later vendored
in we transparently switch to :class:`EventSourceResponse` for finer
flush control — see :func:`_make_response` below.

For tests we expose an *envelope* knob: setting
``PERSONA_SSE_TEST_MAX_EVENTS`` (or passing ``?max_events=N``) makes the
generator terminate after ``N`` frames so ``httpx.AsyncClient`` does not
hang on what is otherwise an infinite stream.
"""

from __future__ import annotations

import json
import math
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import anyio
from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso
from app.workers.control import get_controller

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from anyio.streams.memory import (
        MemoryObjectReceiveStream,
        MemoryObjectSendStream,
    )

EventSourceResponse: Any = None
try:  # pragma: no cover — optional dependency, gracefully degrades
    from sse_starlette.sse import EventSourceResponse as _EventSourceResponse

    EventSourceResponse = _EventSourceResponse
except ImportError:  # pragma: no cover
    EventSourceResponse = None

log = get_logger("persona.sse")
live_count_log = get_logger("persona.live_count")

router = APIRouter(prefix="", tags=["live-sse"])


# --- broadcast plumbing ------------------------------------------------------

_BROADCAST_BUFFER: Final[int] = 16
"""Per-subscriber queue depth. Slow clients are dropped rather than
back-pressuring the heartbeat publishers."""

STATUS_TICK_SECONDS: Final[float] = 2.0
"""Interval at which the route emits a fresh ``status`` event."""

_subscribers: set[MemoryObjectSendStream[dict[str, Any]]] = set()
_subscribers_lock = anyio.Lock()


@asynccontextmanager
async def _subscribe() -> AsyncIterator[MemoryObjectReceiveStream[dict[str, Any]]]:
    """Register a new subscriber and yield its receive stream.

    The send half is owned by the broadcaster (kept alive in
    ``_subscribers``); the receive half lives for the duration of the
    HTTP connection.
    """
    send, receive = anyio.create_memory_object_stream[dict[str, Any]](
        max_buffer_size=_BROADCAST_BUFFER
    )
    async with _subscribers_lock:
        _subscribers.add(send)
    try:
        yield receive
    finally:
        async with _subscribers_lock:
            _subscribers.discard(send)
        await send.aclose()
        await receive.aclose()


async def publish_heartbeat(worker_name: str, last_run_at: datetime) -> None:
    """Broadcast a worker heartbeat to every active SSE connection.

    Safe to call from any task. Subscribers whose queue is full are
    silently skipped — we never block a worker on a slow client.
    """
    payload = {
        "type": "heartbeat",
        "payload": {
            "worker_name": worker_name,
            "last_run_at": iso(last_run_at),
        },
    }
    async with _subscribers_lock:
        targets = list(_subscribers)
    for stream in targets:
        try:
            stream.send_nowait(payload)
        except anyio.WouldBlock:
            log.debug("sse.drop_slow_subscriber", worker=worker_name)
        except anyio.BrokenResourceError:
            log.debug("sse.broken_subscriber", worker=worker_name)


# --- status snapshot ---------------------------------------------------------


async def _today_shots_count() -> int:
    """Number of screenshots captured since 00:00 UTC today.

    A failed DB read returns ``0`` so the pill never goes blank on a
    transient hiccup.
    """
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM screenshots "
                "WHERE captured_at >= ? AND captured_at < ?",
                (iso(today), iso(tomorrow)),
            )
            row = await cursor.fetchone()
    except Exception as exc:  # pragma: no cover — best-effort read
        log.warning("sse.today_shots_failed", error=str(exc))
        return 0
    return int(row["n"]) if row else 0


async def _ocr_pending_count() -> int:
    """Rows currently waiting for the OCR worker."""
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM screenshots WHERE ocr_status = 'pending'"
            )
            row = await cursor.fetchone()
    except Exception as exc:  # pragma: no cover — best-effort read
        log.warning("sse.ocr_pending_failed", error=str(exc))
        return 0
    return int(row["n"]) if row else 0


async def _total_shots_count() -> int:
    """All-time screenshot count powering the dashboard live-count widget.

    The widget (v0.87) shows the running total of every shot ever
    captured, refreshed on the same two-second status tick as the
    header pill. A failed read returns ``0`` so the dashboard tile
    never goes blank on a transient hiccup — the next tick will
    correct it.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots")
            row = await cursor.fetchone()
    except Exception as exc:  # pragma: no cover — best-effort read
        live_count_log.warning("total_shots_failed", error=str(exc))
        return 0
    return int(row["n"]) if row else 0


async def _build_status_snapshot() -> dict[str, Any]:
    controller = get_controller()
    today_shots = await _today_shots_count()
    ocr_pending = await _ocr_pending_count()
    total_shots = await _total_shots_count()
    live_count_log.debug("status_snapshot", total_shots=total_shots)
    return {
        "type": "status",
        "payload": {
            "capture_running": not controller.paused,
            "ocr_pending": ocr_pending,
            "last_capture_at": (
                iso(controller.last_capture_at) if controller.last_capture_at else None
            ),
            "today_shots": today_shots,
            "total_shots": total_shots,
        },
    }


# --- wire format -------------------------------------------------------------


def _encode_sse(event: dict[str, Any]) -> bytes:
    """Encode one event in the SSE ``data: <json>\\n\\n`` wire format."""
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()


def _resolve_test_limit(request_max: int | None) -> int | None:
    """Pick the smaller of the env-var cap and the query-string cap.

    Either knob being unset means "no cap from that side". Returning
    ``None`` keeps the stream infinite (production behaviour).
    """
    env_raw = os.getenv("PERSONA_SSE_TEST_MAX_EVENTS")
    env_cap: int | None = None
    if env_raw:
        try:
            env_cap = max(0, int(env_raw))
        except ValueError:
            env_cap = None
    candidates = [c for c in (env_cap, request_max) if c is not None]
    if not candidates:
        return None
    return min(candidates)


# --- event generator ---------------------------------------------------------


async def _event_stream(
    request: Request, max_events: int | None
) -> AsyncIterator[bytes]:
    """Yield SSE frames until the client disconnects or the cap is hit.

    The status ticker runs in a dedicated task so heartbeats published
    from worker code interleave naturally without waiting for the next
    two-second beat.
    """
    limit = _resolve_test_limit(max_events)
    emitted = 0

    async with _subscribe() as receive:

        async def _tick_status(
            sink: MemoryObjectSendStream[dict[str, Any]],
        ) -> None:
            try:
                while True:
                    snapshot = await _build_status_snapshot()
                    try:
                        await sink.send(snapshot)
                    except anyio.BrokenResourceError:
                        return
                    await anyio.sleep(STATUS_TICK_SECONDS)
            except anyio.get_cancelled_exc_class():
                raise

        # Local pipe so the status ticker can push into the same merged
        # queue our outer loop drains; we never share a sink with the
        # broadcast set (heartbeats go through ``receive`` directly).
        local_send, local_receive = anyio.create_memory_object_stream[dict[str, Any]](
            max_buffer_size=4
        )

        async with anyio.create_task_group() as tg:

            async def _drain_broadcast() -> None:
                try:
                    async for event in receive:
                        try:
                            await local_send.send(event)
                        except anyio.BrokenResourceError:
                            return
                except anyio.EndOfStream:
                    return

            tg.start_soon(_tick_status, local_send)
            tg.start_soon(_drain_broadcast)

            try:
                async for event in local_receive:
                    if await request.is_disconnected():
                        break
                    yield _encode_sse(event)
                    emitted += 1
                    if limit is not None and emitted >= limit:
                        break
            finally:
                tg.cancel_scope.cancel()
                await local_send.aclose()
                await local_receive.aclose()


# --- HTTP entry point --------------------------------------------------------


def _make_response(
    request: Request, max_events: int | None
) -> StreamingResponse:
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    if EventSourceResponse is not None:  # pragma: no cover — opt-in path
        # sse_starlette already manages the content-type + flushing, but
        # it expects an async-iterable of *dicts*, not bytes. The raw
        # generator above is the truth-source; we adapt by stripping
        # the wire framing.
        async def _dict_stream() -> AsyncIterator[dict[str, Any]]:
            async for frame in _event_stream(request, max_events):
                # ``frame`` is ``data: {json}\n\n`` — unwrap to the dict.
                try:
                    payload = frame.decode("utf-8").removeprefix("data: ").strip()
                    yield {"data": payload}
                except Exception as exc:  # pragma: no cover
                    # Malformed frames are best-effort skipped; we log so
                    # repeated decode failures show up in operator logs.
                    log.debug("sse.frame_decode_failed", error=str(exc))
                    continue

        return EventSourceResponse(_dict_stream(), headers=headers)  # type: ignore[no-any-return]

    return StreamingResponse(
        _event_stream(request, max_events),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/events")
async def stream_events(request: Request, max_events: int | None = None) -> StreamingResponse:
    """Long-lived SSE stream powering the header status pill.

    Query params:
        max_events: optional cap (used by tests). Combined with the
            ``PERSONA_SSE_TEST_MAX_EVENTS`` env var via ``min()``.
    """
    # Negative or NaN caps reduce to "no cap".
    safe_max: int | None
    if max_events is None or max_events < 0 or (
        isinstance(max_events, float) and math.isnan(max_events)
    ):
        safe_max = None
    else:
        safe_max = int(max_events)
    log.debug("sse.connect", path="/events", max_events=safe_max)
    return _make_response(request, safe_max)


__all__ = [
    "STATUS_TICK_SECONDS",
    "publish_heartbeat",
    "router",
    "stream_events",
]
