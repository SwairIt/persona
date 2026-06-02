"""Bring-Your-Own-API-Key LLM client. We never see the user's key."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.settings import get_settings

Provider = Literal["anthropic", "openai", "groq"]


class LLMNotConfigured(RuntimeError):
    """Raised when BYO API key is missing or provider unsupported."""


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    system: str
    user: str
    max_tokens: int = 800
    temperature: float = 0.4


class LLMClient(Protocol):
    provider: Provider

    async def complete(self, request: CompletionRequest) -> str: ...


class AnthropicClient:
    provider: Provider = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.anthropic.com/v1/messages"

    async def complete(self, request: CompletionRequest) -> str:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(self._base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        for block in data.get("content", []):
            if block.get("type") == "text":
                return str(block.get("text", "")).strip()
        return ""


class OpenAIClient:
    provider: Provider = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.openai.com/v1/chat/completions"

    async def complete(self, request: CompletionRequest) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(self._base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices", [])
        if not choices:
            return ""
        return str(choices[0]["message"]["content"]).strip()


class GroqClient:
    provider: Provider = "groq"

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.groq.com/openai/v1/chat/completions"

    async def complete(self, request: CompletionRequest) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(self._base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices", [])
        if not choices:
            return ""
        return str(choices[0]["message"]["content"]).strip()


def make_client(
    provider: Provider | None = None,
    api_key: str | None = None,
) -> LLMClient:
    """Construct a client from settings (or explicit args).

    BYO key model: the API key NEVER touches the Persona backend in transit
    other than briefly in-memory during the request. We don't log it, persist
    it server-side, or expose it via any endpoint.
    """
    cfg = get_settings()
    use_provider = provider or cfg.byo_api_provider.strip().lower() or None
    use_key = api_key or cfg.byo_api_key.strip() or None

    if not use_provider or not use_key:
        msg = (
            "LLM not configured. Set PERSONA_BYO_API_PROVIDER (anthropic|openai|groq) "
            "and PERSONA_BYO_API_KEY in .env to enable AI features."
        )
        raise LLMNotConfigured(msg)

    if use_provider == "anthropic":
        return AnthropicClient(use_key)
    if use_provider == "openai":
        return OpenAIClient(use_key)
    if use_provider == "groq":
        return GroqClient(use_key)

    msg = f"Unsupported LLM provider: {use_provider}"
    raise LLMNotConfigured(msg)
