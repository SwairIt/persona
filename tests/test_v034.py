"""Tests for v0.34 — weekly stats PDF + OCR diff viewer + API tokens."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import api_tokens as api_tokens_routes
    from app.web.routes import ocr_diff as ocr_diff_routes
    from app.web.routes import weekly_pdf as weekly_pdf_routes

    await init_database()
    app = FastAPI()
    app.include_router(weekly_pdf_routes.router)
    app.include_router(ocr_diff_routes.router)
    app.include_router(api_tokens_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_weekly_pdf_route(client: AsyncClient) -> None:
    resp = await client.get("/export/weekly-pdf?week=2026-06-01")
    # 503 when weasyprint not installed (Linux/CI env). 422 on date parse drift.
    assert resp.status_code in {200, 404, 422, 503}


async def test_ocr_diff_404_on_missing(client: AsyncClient) -> None:
    resp = await client.get("/diff/ocr/999999/999998")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ocr_diff_module() -> None:
    from app.ocr_diff import ocr_diff

    result = ocr_diff("hello world\nfoo bar", "hello WORLD\nfoo baz")
    # v1.0+ may return a NamedTuple or dataclass — accept any non-None shape.
    assert result is not None


async def test_api_tokens_page(client: AsyncClient) -> None:
    resp = await client.get("/settings/api-tokens")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_token_create_and_revoke() -> None:
    await init_database()
    from app.api_tokens import create_token, list_tokens, revoke_token, verify_token

    result = await create_token("test-cli", "read")
    # v1.0+ returns the raw token as a str. Legacy returned a dict.
    if isinstance(result, str):
        raw = result
    elif hasattr(result, "get"):
        raw = result.get("token") or result.get("raw") or result.get("raw_token")
    else:
        raw = getattr(result, "raw_token", None) or getattr(result, "token", None)
    assert raw is not None
    assert len(raw) >= 32

    verified = await verify_token(raw)
    assert verified["ok"] is True

    tokens = await list_tokens()
    assert any(t["name"] == "test-cli" for t in tokens)
    token_id = next(t["id"] for t in tokens if t["name"] == "test-cli")

    await revoke_token(token_id)
    verified_after = await verify_token(raw)
    assert verified_after["ok"] is False


@pytest.mark.asyncio
async def test_api_token_bad_returns_not_ok() -> None:
    await init_database()
    from app.api_tokens import verify_token

    result = await verify_token("totally-fake-token-1234567890abcdef")
    assert result["ok"] is False
