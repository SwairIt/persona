"""Contract tests for the dependency-free process liveness probe."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.web.routes import health as health_routes
from app.web.routes.setup_gate import _is_allowed

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(health_routes.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http_client:
        yield http_client


async def test_healthz_is_cheap_and_does_not_touch_database(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_database_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("/healthz must not access the database")

    monkeypatch.setattr(health_routes, "get_connection", unexpected_database_access)

    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["cache-control"] == "no-store"


def test_healthz_bypasses_first_run_setup_redirect() -> None:
    assert _is_allowed("/healthz") is True
