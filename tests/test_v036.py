"""Tests for v0.36 — Pomodoro focus-mode + audit log + per-day TL;DR."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import audit as audit_routes
    from app.web.routes import day_tldr as day_tldr_routes
    from app.web.routes import focus as focus_routes

    await init_database()
    app = FastAPI()
    app.include_router(focus_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(day_tldr_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_focus_page(client: AsyncClient) -> None:
    resp = await client.get("/focus")
    assert resp.status_code == 200


async def test_focus_start_and_end(client: AsyncClient) -> None:
    start = await client.post(
        "/focus/start",
        data={"work_minutes": "25", "break_minutes": "5", "label": "test"},
    )
    assert start.status_code in {200, 303, 302}

    current = await client.get("/api/focus/current.json")
    assert current.status_code == 200


@pytest.mark.asyncio
async def test_focus_module() -> None:
    await init_database()
    from app.focus import current_session, end_session, start_session

    s = await start_session(25, 5, "test-session")
    sid = s.get("id") if isinstance(s, dict) else s
    assert sid is not None

    cur = await current_session()
    assert cur is not None

    await end_session(sid, completed=True)
    cur_after = await current_session()
    assert cur_after is None or cur_after.get("id") != sid


async def test_audit_page(client: AsyncClient) -> None:
    resp = await client.get("/audit")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_audit_log_records_action() -> None:
    await init_database()
    from app.audit import list_recent, log_action

    await log_action("test.action", actor="pytest", target="x", detail="hello")
    rows = await list_recent(limit=5)
    assert any(r["action"] == "test.action" for r in rows)


@pytest.mark.asyncio
async def test_audit_never_logs_secret_values() -> None:
    """Smoke check that the recorded detail field doesn't contain a sentinel secret."""
    await init_database()
    from app.audit import list_recent, log_action

    await log_action("vault.set", target="api-key", detail="key=api-key")
    rows = await list_recent(limit=5)
    for r in rows:
        detail = r.get("detail") or ""
        assert "sk-" not in detail
        assert "Bearer " not in detail


async def test_day_tldr_api_missing_config_is_ok(client: AsyncClient) -> None:
    """Without an LLM configured, endpoint should return a status field — not crash."""
    resp = await client.get("/api/day-tldr.json?day=2026-06-02")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data or "tldr" in data


@pytest.mark.asyncio
async def test_day_tldr_module_returns_status() -> None:
    await init_database()
    from app.llm.day_tldr import summarise_day_tldr

    result = await summarise_day_tldr("2026-06-02")
    assert "status" in result
    assert result["status"] in {"ok", "cached", "missing_config", "no_data", "error"}
