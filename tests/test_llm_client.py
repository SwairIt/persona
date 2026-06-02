"""Tests for BYO LLM client factory — no network calls."""

from __future__ import annotations

import pytest

from app.llm import LLMNotConfigured, make_client
from app.llm.client import AnthropicClient, GroqClient, OpenAIClient
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
    assert isinstance(client, AnthropicClient)


def test_make_client_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_BYO_API_KEY", "sk-fake")
    monkeypatch.setenv("PERSONA_BYO_API_PROVIDER", "openai")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    client = make_client()
    assert isinstance(client, OpenAIClient)


def test_make_client_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_BYO_API_KEY", "gsk-fake")
    monkeypatch.setenv("PERSONA_BYO_API_PROVIDER", "groq")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    client = make_client()
    assert isinstance(client, GroqClient)


def test_make_client_unsupported_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_BYO_API_KEY", "fake")
    monkeypatch.setenv("PERSONA_BYO_API_PROVIDER", "bedrock")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(LLMNotConfigured):
        make_client()


def test_make_client_explicit_args() -> None:
    client = make_client(provider="anthropic", api_key="sk-ant-direct")
    assert isinstance(client, AnthropicClient)
