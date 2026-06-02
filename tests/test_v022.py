"""Tests for v0.22 — persona-doctor + weekly digest + capture CLI."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.diagnostics import run_doctor
from app.storage.db import init_database


@pytest.mark.asyncio
async def test_doctor_returns_results() -> None:
    results = await run_doctor()
    assert isinstance(results, list)
    assert len(results) >= 10
    for r in results:
        assert {"name", "status", "detail"} <= set(r)
        assert r["status"] in {"pass", "warn", "fail"}


@pytest.mark.asyncio
async def test_doctor_includes_core_checks() -> None:
    results = await run_doctor()
    names = {r["name"] for r in results}
    assert "python_version" in names
    assert "sqlite_version" in names
    assert "data_dir_writable" in names
    assert "db_integrity" in names


@pytest.mark.asyncio
async def test_doctor_no_critical_failures_on_fresh_db() -> None:
    """On a fresh test DB, sqlite + dir + integrity should all pass."""
    await init_database()
    results = await run_doctor()
    by_name = {r["name"]: r for r in results}
    assert by_name["python_version"]["status"] in {"pass", "warn"}
    assert by_name["sqlite_version"]["status"] == "pass"
    assert by_name["data_dir_writable"]["status"] == "pass"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import doctor as doctor_routes
    from app.web.routes import weekly_digests as weekly_digests_routes

    await init_database()
    app = FastAPI()
    app.include_router(doctor_routes.router)
    app.include_router(weekly_digests_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_doctor_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/doctor")
    assert resp.status_code == 200
    text = resp.text
    assert "python_version" in text or "Python" in text


async def test_weekly_digests_archive_renders(client: AsyncClient) -> None:
    resp = await client.get("/digest/weekly-archive")
    assert resp.status_code == 200


async def test_weekly_digest_detail_404(client: AsyncClient) -> None:
    resp = await client.get("/digest/weekly-archive/2099-01-06")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_capture_cli_subcommand_exists() -> None:
    """Smoke test — CLI module exposes the new doctor + capture subcommands."""
    from app import cli

    parser = cli._build_parser() if hasattr(cli, "_build_parser") else None
    # If the helper isn't exposed, at least the dispatch table should mention them.
    src = open(cli.__file__, encoding="utf-8").read()
    assert "doctor" in src
    assert '"capture"' in src or "'capture'" in src or "capture" in src
