"""web_search: keyless DuckDuckGo/Openverse fallback + Brave preference (2026-07-31).

The owner had no working search at all — Brave was the only provider and no
key existed anywhere. These tests mock the HTTP layer (never call the live
internet) and cover: keyless fallback parsing, Brave preference when a key
is configured, fallback-on-Brave-error, and clean [error] strings on
malformed provider responses.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest

from app.mcp.builtin_tools import verify_media_url, web_search

DDG_HTML = """
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fcats&amp;rut=abc">
         Cat GIFs - Find &amp; Share</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fcats">
      Great cat gifs here.</a>
  </div>
</div>
"""


class _FakeResponse:
    def __init__(self, *, text: str = "", json_data: Any = None, status_code: int = 200,
                 headers: dict[str, str] | None = None) -> None:
        self.text = text
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class _FakeAsyncClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, get_impl=None, head_impl=None, **_kw: Any) -> None:
        self._get_impl = get_impl
        self._head_impl = head_impl

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> _FakeResponse:
        if self._get_impl is None:
            raise AssertionError("unexpected GET " + url)
        return self._get_impl(url, **kw)

    async def head(self, url: str, **kw: Any) -> _FakeResponse:
        if self._head_impl is None:
            raise AssertionError("unexpected HEAD " + url)
        return self._head_impl(url, **kw)


async def _no_kv_key(db: aiosqlite.Connection) -> None:
    """Ensure no Brave key exists in kv_settings for this test's connection."""
    await db.execute("DELETE FROM kv_settings WHERE key IN ('byo_api_key_brave','brave_api_key')")
    await db.commit()


@pytest.mark.asyncio
async def test_keyless_fallback_parses_duckduckgo_html(db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    await _no_kv_key(db)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("PERSONA_BRAVE_API_KEY", raising=False)

    def fake_get(url: str, **kw: Any) -> _FakeResponse:
        assert "duckduckgo.com" in url
        return _FakeResponse(text=DDG_HTML)

    with patch("httpx.AsyncClient", lambda **kw: _FakeAsyncClient(get_impl=fake_get)):
        out = await web_search({"query": "cat gif"})

    assert out.startswith("[ok] поиск")
    assert "example.com/cats" in out
    assert "Cat GIFs" in out


@pytest.mark.asyncio
async def test_brave_used_when_key_configured(db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "test-key-123")
    calls: list[str] = []

    def fake_get(url: str, **kw: Any) -> _FakeResponse:
        calls.append(url)
        assert "api.search.brave.com" in url
        return _FakeResponse(json_data={"web": {"results": [
            {"title": "Cats", "url": "https://example.com/cats", "description": "meow"},
        ]}})

    with patch("httpx.AsyncClient", lambda **kw: _FakeAsyncClient(get_impl=fake_get)):
        out = await web_search({"query": "cats"})

    assert out.startswith("[ok] поиск")
    assert "example.com/cats" in out
    assert calls and "brave.com" in calls[0]


@pytest.mark.asyncio
async def test_brave_error_falls_through_to_keyless(db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "test-key-123")

    def fake_get(url: str, **kw: Any) -> _FakeResponse:
        if "brave.com" in url:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
        assert "duckduckgo.com" in url
        return _FakeResponse(text=DDG_HTML)

    with patch("httpx.AsyncClient", lambda **kw: _FakeAsyncClient(get_impl=fake_get)):
        out = await web_search({"query": "cat gif"})

    assert out.startswith("[ok] поиск")
    assert "example.com/cats" in out


@pytest.mark.asyncio
async def test_malformed_provider_response_yields_clean_error(db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    await _no_kv_key(db)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("PERSONA_BRAVE_API_KEY", raising=False)

    def fake_get(url: str, **kw: Any) -> _FakeResponse:
        raise ValueError("malformed upstream response")

    with patch("httpx.AsyncClient", lambda **kw: _FakeAsyncClient(get_impl=fake_get)):
        out = await web_search({"query": "cat gif"})

    assert out.startswith("[error]")


@pytest.mark.asyncio
async def test_verify_media_url_rejects_html_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_head(url: str, **kw: Any) -> _FakeResponse:
        return _FakeResponse(status_code=200, headers={"content-type": "text/html; charset=utf-8"})

    # The sandbox's DNS may resolve example.com to a private/reserved
    # address (no real internet egress) — pin resolution to a public IP so
    # the SSRF allowlist check (_url_is_safe) doesn't false-positive here;
    # the point of this test is the content-type rejection, not DNS.
    def fake_getaddrinfo(host: str, *a: Any, **kw: Any) -> list[tuple]:
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    with patch("httpx.AsyncClient", lambda **kw: _FakeAsyncClient(get_impl=fake_head, head_impl=fake_head)):
        out = await verify_media_url({"url": "https://example.com/not-an-image"})

    assert out.startswith("[error]")
    assert "text/html" in out
