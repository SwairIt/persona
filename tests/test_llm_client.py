"""Tests for BYO LLM client factory — no network calls.

v0.98: ``make_client`` now returns a :class:`_UsageRecordingClient`
wrapper instead of the bare provider client so the ledger row in
``llm_usage`` gets written on every completion. The provider identity
is asserted via the ``.provider`` attribute on the wrapper rather than
``isinstance`` because the wrapper is the concrete return type now.
"""

from __future__ import annotations

import pytest

from app.llm import LLMNotConfigured, make_client
from app.llm.client import (
    AnthropicClient,
    GroqClient,
    OpenAIClient,
    _UsageRecordingClient,
)
from app.settings import get_settings


def test_make_client_unconfigured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_BYO_API_KEY", "")
    monkeypatch.setenv("PERSONA_BYO_API_PROVIDER", "")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(LLMNotConfigured):
        make_client()


def test_make_client_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_BYO_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("PERSONA_BYO_API_PROVIDER", "anthropic")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    client = make_client()
    assert isinstance(client, _UsageRecordingClient)
    assert client.provider == "anthropic"
    assert isinstance(client._inner, AnthropicClient)


def test_make_client_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_BYO_API_KEY", "sk-fake")
    monkeypatch.setenv("PERSONA_BYO_API_PROVIDER", "openai")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    client = make_client()
    assert isinstance(client, _UsageRecordingClient)
    assert client.provider == "openai"
    assert isinstance(client._inner, OpenAIClient)


def test_make_client_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_BYO_API_KEY", "gsk-fake")
    monkeypatch.setenv("PERSONA_BYO_API_PROVIDER", "groq")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    client = make_client()
    assert isinstance(client, _UsageRecordingClient)
    assert client.provider == "groq"
    assert isinstance(client._inner, GroqClient)


def test_make_client_unsupported_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_BYO_API_KEY", "fake")
    monkeypatch.setenv("PERSONA_BYO_API_PROVIDER", "bedrock")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(LLMNotConfigured):
        make_client()


def test_make_client_explicit_args() -> None:
    client = make_client(provider="anthropic", api_key="sk-ant-direct")
    assert isinstance(client, _UsageRecordingClient)
    assert client.provider == "anthropic"
    assert isinstance(client._inner, AnthropicClient)
