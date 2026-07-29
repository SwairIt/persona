from __future__ import annotations

import asyncio
import json

from app.web.main import create_app
from app.web.routes import copilot


def test_owner_routes_keep_runtime_session_dependencies() -> None:
    app = create_app()
    paths = {
        route.path: route
        for route in app.routes
        if getattr(route, "path", "") in {
            "/api/copilot/ask",
            "/settings/system-prompt/history",
        }
    }
    assert set(paths) == {
        "/api/copilot/ask",
        "/settings/system-prompt/history",
    }
    for route in paths.values():
        assert any(item.name == "session" for item in route.dependant.dependencies)
        assert all(item.name != "session" for item in route.dependant.query_params)


async def test_copilot_sends_heartbeat_while_worker_is_slow(monkeypatch) -> None:
    async def slow_stream(*_args, **_kwargs):
        yield {"type": "meta", "mode": "ask"}
        await asyncio.sleep(0.05)
        yield {"type": "done", "full_answer": "ok"}

    monkeypatch.setattr(copilot, "stream_copilot", slow_stream)
    monkeypatch.setattr(copilot, "_HEARTBEAT_SECONDS", 0.01)
    stream = copilot._event_stream("q", "/", "ask", 1)

    first = await anext(stream)
    frames = [await anext(stream) for _ in range(5)]
    await stream.aclose()

    assert json.loads(first.removeprefix(b"data: ").strip())["type"] == "meta"
    assert any(frame.startswith(b": persona-copilot-ping") for frame in frames)
    event_frames = [frame for frame in frames if frame.startswith(b"data: ")]
    assert json.loads(event_frames[-1].removeprefix(b"data: ").strip())["type"] == (
        "done"
    )
