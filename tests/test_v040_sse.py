"""Tests for v0.40 — SSE-driven live status pill (feature 1/3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database
from app.web.routes import live_sse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    await init_database()
    app = FastAPI()
    app.include_router(live_sse.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _parse_sse_frames(body: str) -> list[dict[str, object]]:
    """Pull every ``data: {...}`` JSON payload out of an SSE response."""
    frames: list[dict[str, object]] = []
    for raw in body.split("\n\n"):
        chunk = raw.strip()
        if not chunk.startswith("data:"):
            continue
        payload = chunk.removeprefix("data:").strip()
        if not payload:
            continue
        frames.append(json.loads(payload))
    return frames


async def test_events_emits_status_frame(client: AsyncClient) -> None:
    """One status frame is enough to prove the pipe is wired up."""
    resp = await client.get("/events?max_events=1")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"].startswith("no-cache")
    assert resp.headers["x-accel-buffering"] == "no"

    frames = _parse_sse_frames(resp.text)
    assert len(frames) == 1
    status = frames[0]
    assert status["type"] == "status"
    payload = status["payload"]
    assert isinstance(payload, dict)
    assert "capture_running" in payload
    assert "ocr_pending" in payload
    assert "today_shots" in payload
    assert "last_capture_at" in payload


async def test_events_respects_env_cap(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PERSONA_SSE_TEST_MAX_EVENTS`` caps the stream length too."""
    monkeypatch.setenv("PERSONA_SSE_TEST_MAX_EVENTS", "1")
    resp = await client.get("/events")
    assert resp.status_code == 200
    frames = _parse_sse_frames(resp.text)
    assert len(frames) == 1


async def test_heartbeat_publishes_to_active_stream() -> None:
    """``publish_heartbeat`` reaches an in-flight subscriber.

    We exercise the broadcast plumbing directly (no HTTP) so the test
    stays deterministic and never sleeps on the 2-second tick.
    """
    async with live_sse._subscribe() as receive:
        await live_sse.publish_heartbeat("test-worker", datetime.now(UTC))
        event = await receive.receive()
    assert event["type"] == "heartbeat"
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["worker_name"] == "test-worker"
    assert isinstance(payload["last_run_at"], str)


async def test_heartbeat_with_no_subscribers_is_noop() -> None:
    """Calling ``publish_heartbeat`` with zero subscribers must not raise."""
    await live_sse.publish_heartbeat("idle-worker", datetime.now(UTC))


async def test_build_status_snapshot_shape() -> None:
    """The status builder returns the documented payload keys."""
    await init_database()
    event = await live_sse._build_status_snapshot()
    assert event["type"] == "status"
    payload = event["payload"]
    assert isinstance(payload, dict)
    for key in ("capture_running", "ocr_pending", "last_capture_at", "today_shots"):
        assert key in payload
    assert isinstance(payload["capture_running"], bool)
    assert isinstance(payload["ocr_pending"], int)
    assert isinstance(payload["today_shots"], int)


async def test_live_status_js_exists_and_uses_eventsource() -> None:
    js = Path("C:/www-Yaroslav/Persona/app/web/static/live_status.js")
    assert js.exists()
    content = js.read_text(encoding="utf-8")
    assert "EventSource" in content
    assert "/events" in content
    assert "status-pill" in content


async def test_base_html_references_sse_script() -> None:
    tpl = Path("C:/www-Yaroslav/Persona/app/web/templates/base.html")
    assert tpl.exists()
    content = tpl.read_text(encoding="utf-8")
    assert "/static/live_status.js" in content
    # The 5-second poll must be gone — that was the whole point of v0.40.
    assert "setInterval(() => this.refresh(), 5000)" not in content
