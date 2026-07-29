"""Bring-Your-Own-API-Key LLM client. We never see the user's key."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from app.logging_setup import get_logger
from app.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

Provider = Literal[
    "anthropic",
    "openai",
    "groq",
    "gemini",
    # T9 (2026-06-07) — Russia-accessible providers. Yandex + Sber
    # don't require VPN or foreign cards, and both have a free tier
    # large enough for normal daily use. DeepSeek is also accessible
    # from Russia and is the best $/quality ratio on the market.
    "yandex",
    "gigachat",
    "deepseek",
    # T10 (2026-06-07) — local-first Ollama. Talks to a model running on
    # the same machine (or the user's LAN). Free forever, no API key,
    # no rate limit, no data leaves the device. The right answer when
    # the user explicitly wants "AI that's mine and free".
    "ollama",
    # T12 (2026-06-07) — six more providers covering the modern field.
    # All are OpenAI-compatible at the wire level so each is a thin
    # subclass of the same Bearer-token + JSON chat-completions client.
    "openrouter",   # international aggregator, 400+ models behind one key
    "mistral",      # EU, free tier, strong open-weight models
    "together",     # huge open-weight catalogue, $25 free credit
    "xai",          # Grok (Elon's), works fine for non-rude prompts
    "proxyapi",     # Russian gateway to OpenAI/Anthropic/Gemini/Claude in RUB
    "aitunnel",     # Russian aggregator alternative to proxyapi
    # W-B (2026-06-30) — «Persona LLM Worker»: вместо devtunnel ПК делает
    # ИСХОДЯЩИЕ запросы к серверу и забирает задачи из очереди в БД, считает
    # на локальной Ollama без туннеля и шлёт результат обратно по HTTP.
    # С точки зрения сервера это ещё один провайдер: задачи кладутся в очередь
    # (app.llm.worker_queue), а WorkerLLMClient.stream поллит готовые чанки.
    "worker",
]

log = get_logger("persona.llm.switcher")
usage_log = get_logger("persona.llm_usage")

# ---------------------------------------------------------------------------
# kv_setting + vault key names (mirrored from app.web.routes.llm_switcher)
# ---------------------------------------------------------------------------

#: v0.71 chosen-provider row. Falls back to ``byo_api_provider`` (v0.50
#: wizard) and finally to ``anthropic`` so an upgraded install with no
#: explicit choice still talks to the most common provider.
_KV_LLM_PROVIDER = "llm_provider"
_KV_LEGACY_PROVIDER = "byo_api_provider"

#: Legacy single-key row written by the v0.50 setup wizard.
_KV_LEGACY_KEY = "byo_api_key"


def _vault_key_for(provider: str) -> str:
    """Vault row name used by the v0.71 switcher."""
    return f"llm_api_key_{provider}"


def _kv_fallback_key_for(provider: str) -> str:
    """kv_settings row used when the vault is unavailable."""
    return f"byo_api_key_{provider}"


class LLMNotConfigured(RuntimeError):
    """Raised when BYO API key is missing or provider unsupported."""


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    system: str
    user: str
    max_tokens: int = 800
    temperature: float = 0.4
    # T22.2 (2026-06-08) — optional image attachment for vision-capable
    # providers (Ollama llava/moondream/qwen-vl, Gemini, Claude, GPT-4o).
    # Format: data URL ``data:image/png;base64,iVBORw0...``. Non-vision
    # providers will silently ignore the image and answer the text only.
    image_data_url: str | None = None


class LLMClient(Protocol):
    provider: Provider

    async def complete(self, request: CompletionRequest) -> str: ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Yield incremental text deltas as the provider produces them.

        Returns an async iterator of plain string chunks (the new text
        produced since the last yield, not the full running answer).
        Implementations that fail to open a streaming connection fall
        back to a single ``complete()`` call and yield the result as
        one chunk so the caller can treat ``.stream()`` as the universal
        path.
        """
        ...


def _anthropic_system(system: str) -> object:
    """Системный промпт для Anthropic с prompt-caching.

    Оборачиваем system в один text-блок с ``cache_control:{type:"ephemeral"}``,
    чтобы Anthropic кэшировал префикс (persona+правила+инструменты): повторные
    ходы в той же сессии с тем же префиксом читаются из кэша (~0.1× цены и
    меньше латентности). Безопасно: содержимое то же; если префикс короче
    минимума модели — Anthropic просто молча не кэширует (не ошибка). Пустой
    system → отдаём пустую строку (блок с пустым текстом слать нельзя).
    """
    if not system:
        return ""
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


class AnthropicClient:
    provider: Provider = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.anthropic.com/v1/messages"
        #: Token counts pulled out of the most recent response payload so
        #: the :class:`_UsageRecordingClient` wrapper can persist a row
        #: without re-parsing the body. ``None`` until the first call,
        #: and reset on every :meth:`complete` invocation so a stale
        #: count from a previous request cannot leak forward.
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None
        #: Prompt-cache учёт из usage ответа (None пока не было вызова, сброс
        #: в начале каждого, чтобы старое значение не утекло вперёд).
        self.last_cache_read_tokens: int | None = None
        self.last_cache_creation_tokens: int | None = None

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
        self.last_cache_read_tokens = None
        self.last_cache_creation_tokens = None
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": _anthropic_system(request.system),
            "messages": [{"role": "user", "content": request.user}],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(self._base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        usage = data.get("usage") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("input_tokens"))
        self.last_output_tokens = _coerce_token_count(usage.get("output_tokens"))
        self.last_cache_read_tokens = _coerce_token_count(usage.get("cache_read_input_tokens"))
        self.last_cache_creation_tokens = _coerce_token_count(
            usage.get("cache_creation_input_tokens")
        )
        if self.last_cache_read_tokens or self.last_cache_creation_tokens:
            usage_log.info(
                "llm.anthropic.cache", model=self._model,
                cache_read=self.last_cache_read_tokens,
                cache_creation=self.last_cache_creation_tokens,
            )

        for block in data.get("content", []):
            if block.get("type") == "text":
                return str(block.get("text", "")).strip()
        return ""

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Stream incremental text deltas from the Anthropic messages API.

        Uses the official server-sent events stream by adding
        ``"stream": true`` to the request body. Each
        ``content_block_delta`` event with a ``text_delta`` block carries
        a new chunk of text; ``message_delta`` carries the final usage
        accounting which we stash for the wrapper to persist.
        """
        self.last_input_tokens = None
        self.last_output_tokens = None
        self.last_cache_read_tokens = None
        self.last_cache_creation_tokens = None
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        payload = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": _anthropic_system(request.system),
            "messages": [{"role": "user", "content": request.user}],
            "stream": True,
        }
        try:
            async with (
                httpx.AsyncClient(timeout=60.0) as client,
                client.stream(
                    "POST", self._base_url, json=payload, headers=headers
                ) as response,
            ):
                response.raise_for_status()
                async for delta in _parse_anthropic_sse(response, self):
                    yield delta
        except Exception as exc:
            log.warning("llm.stream.fallback", provider=self.provider, error=str(exc))
            text = await self.complete(request)
            if text:
                yield text


class OpenAIClient:
    provider: Provider = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.openai.com/v1/chat/completions"
        #: See :class:`AnthropicClient` for the contract. OpenAI emits
        #: ``usage.prompt_tokens`` / ``usage.completion_tokens`` rather
        #: than the Anthropic key names, but we surface both through the
        #: same two ``last_*`` attributes so the wrapper stays uniform.
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
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

        usage = data.get("usage") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("prompt_tokens"))
        self.last_output_tokens = _coerce_token_count(usage.get("completion_tokens"))

        choices = data.get("choices", [])
        if not choices:
            return ""
        return str(choices[0]["message"]["content"]).strip()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Stream deltas from the OpenAI chat.completions API."""
        async for chunk in _stream_openai_compatible(
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            request=request,
            inner=self,
        ):
            yield chunk


class GroqClient:
    provider: Provider = "groq"

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.groq.com/openai/v1/chat/completions"
        #: See :class:`AnthropicClient`. Groq is OpenAI-compatible so the
        #: token keys mirror :class:`OpenAIClient` exactly.
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
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

        usage = data.get("usage") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("prompt_tokens"))
        self.last_output_tokens = _coerce_token_count(usage.get("completion_tokens"))

        choices = data.get("choices", [])
        if not choices:
            return ""
        return str(choices[0]["message"]["content"]).strip()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Stream deltas from the Groq chat.completions API.

        Groq exposes an OpenAI-compatible endpoint so the wire format
        for streaming is identical: ``"stream": true`` plus SSE frames
        carrying ``choices[].delta.content``.
        """
        async for chunk in _stream_openai_compatible(
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            request=request,
            inner=self,
        ):
            yield chunk


class GeminiClient:
    """Google Gemini provider (v1.14).

    Gemini AI Studio offers a genuinely free tier (1M tokens/day,
    1500 requests/day on Flash) with no credit card required. This
    makes it the right default for new Persona installations — the
    user signs up at aistudio.google.com, copies a key, pastes it
    into the setup wizard, and the LLM features work without paying.

    Defaults to ``gemini-2.0-flash`` — fast, cheap (free), good enough
    for hourly card summaries and Q&A. Power users can override the
    model name in settings to ``gemini-2.0-pro`` or future ``2.5``.
    """

    provider: Provider = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash") -> None:
        self._api_key = api_key
        self._model = model
        # Gemini's generative-language API takes the model in the path
        # and the key as a query param. We rebuild the URL on each call
        # so a model change at runtime takes effect immediately.
        self._base = "https://generativelanguage.googleapis.com/v1beta/models"
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
        url = f"{self._base}/{self._model}:generateContent?key={self._api_key}"
        # Gemini wants system as the first "user" role with role split
        # via "systemInstruction" — we use the systemInstruction field
        # so the user/assistant turn structure stays clean.
        payload = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [
                {"role": "user", "parts": [{"text": request.user}]},
            ],
            "generationConfig": {
                "maxOutputTokens": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        usage = data.get("usageMetadata") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("promptTokenCount"))
        self.last_output_tokens = _coerce_token_count(usage.get("candidatesTokenCount"))

        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        if not parts:
            return ""
        return str(parts[0].get("text", "")).strip()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Stream deltas from the Gemini ``streamGenerateContent`` endpoint.

        Gemini exposes streaming via a different method name plus the
        ``?alt=sse`` query flag so the response is SSE rather than the
        default newline-delimited JSON array. Each frame is a partial
        ``GenerateContentResponse`` whose first candidate carries the
        delta text under ``content.parts[].text``.
        """
        self.last_input_tokens = None
        self.last_output_tokens = None
        url = (
            f"{self._base}/{self._model}:streamGenerateContent"
            f"?alt=sse&key={self._api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [
                {"role": "user", "parts": [{"text": request.user}]},
            ],
            "generationConfig": {
                "maxOutputTokens": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        try:
            async with (
                httpx.AsyncClient(timeout=60.0) as client,
                client.stream("POST", url, json=payload) as response,
            ):
                response.raise_for_status()
                async for delta in _parse_gemini_sse(response, self):
                    yield delta
        except Exception as exc:
            log.warning("llm.stream.fallback", provider=self.provider, error=str(exc))
            text = await self.complete(request)
            if text:
                yield text


async def _parse_anthropic_sse(
    response: httpx.Response, client: AnthropicClient
) -> AsyncIterator[str]:
    """Parse an Anthropic messages SSE stream into text deltas.

    Side-effect: stashes the final usage counts onto ``client`` so the
    :class:`_UsageRecordingClient` wrapper can persist a row when the
    stream ends.
    """
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    yield text
        elif event_type == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            client.last_input_tokens = _coerce_token_count(usage.get("input_tokens"))
        elif event_type == "message_delta":
            usage = event.get("usage") or {}
            out_tokens = _coerce_token_count(usage.get("output_tokens"))
            if out_tokens is not None:
                client.last_output_tokens = out_tokens


async def _stream_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    model: str,
    request: CompletionRequest,
    inner: OpenAIClient | GroqClient,
) -> AsyncIterator[str]:
    """Drive an OpenAI-style chat.completions streaming request.

    Used by both :class:`OpenAIClient` and :class:`GroqClient`. Failure
    to open the stream falls back to a single ``.complete()`` call so
    the caller sees one chunk rather than nothing.
    """
    inner.last_input_tokens = None
    inner.last_output_tokens = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    # T22.4 — vision passthrough + WebP-normalisation for Ollama. Inner
    # client's URL tells us which path we're on; if it's localhost or a
    # Tailscale 100.x address we treat as Ollama and normalise.
    user_content: object = request.user
    if request.image_data_url:
        img_url = request.image_data_url
        if "11434" in base_url:  # Ollama endpoint signature
            img_url = _normalise_image_for_ollama(img_url)
        user_content = [
            {"type": "text", "text": request.user or "describe this image"},
            {"type": "image_url", "image_url": {"url": img_url}},
        ]
    payload = {
        "model": model,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": user_content},
        ],
        "stream": True,
        # OpenAI gates per-stream usage on this flag; Groq tolerates it.
        "stream_options": {"include_usage": True},
    }
    # T22.6 (2026-06-08) — fat timeouts for cold-start Ollama on weak
    # hardware. 1050 Ti needs 90-180 sec to load qwen2.5vl:3b into VRAM
    # before producing the first token. Default 60s ate the request
    # before the model even started talking.
    is_ollama_endpoint = "11434" in base_url
    connect_timeout = 10.0
    read_timeout = 600.0 if is_ollama_endpoint else 120.0
    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            ) as client,
            client.stream("POST", base_url, json=payload, headers=headers) as response,
        ):
            if response.status_code >= 400:
                # T22.9 — read body so the actual upstream error makes it
                # into the log instead of just 'Client error 400'.
                body = await response.aread()
                log.warning(
                    "llm.stream.http_error",
                    provider=inner.provider,
                    status=response.status_code,
                    body=body.decode("utf-8", errors="ignore")[:500],
                )
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[len("data:") :].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                usage = event.get("usage") or {}
                if usage:
                    inner.last_input_tokens = _coerce_token_count(
                        usage.get("prompt_tokens")
                    )
                    inner.last_output_tokens = _coerce_token_count(
                        usage.get("completion_tokens")
                    )
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0] or {}).get("delta") or {}
                text = delta.get("content")
                if isinstance(text, str) and text:
                    yield text
    except Exception as exc:
        log.warning("llm.stream.fallback", provider=inner.provider, error=str(exc))
        text = await inner.complete(request)
        if text:
            yield text


class YandexGPTClient:
    """YandexGPT provider — works in Russia without VPN.

    YandexGPT API takes a Yandex IAM token or an API key issued for a
    folder (``Ya.Cloud`` console). We accept either format in the same
    ``api_key`` field — if it starts with ``t1.`` it's an IAM token, else
    treat it as a service-account API key.

    Defaults to ``yandexgpt-lite`` — Yandex's smaller/cheaper variant
    which is plenty for day-summary Q&A. The pro variant is
    ``yandexgpt`` (no ``-lite``).

    Auth header:
        IAM token  → ``Authorization: Bearer t1...``
        API key    → ``Authorization: Api-Key AQVN...``

    Yandex's chat endpoint is at::

        https://llm.api.cloud.yandex.net/foundationModels/v1/completion

    Request shape uses Yandex's own JSON (NOT OpenAI-compatible). See
    https://yandex.cloud/docs/foundation-models/quickstart/yandexgpt
    """

    provider: Provider = "yandex"

    def __init__(
        self,
        api_key: str,
        model: str = "yandexgpt-lite/latest",
        folder_id: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        # The model URI Yandex wants encodes folder_id when an API key
        # is used. We default to the well-known placeholder so the user
        # gets a clear error if they forget to set folder_id.
        self._folder_id = folder_id or "b1g-placeholder"
        self._base_url = (
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
        )
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    def _auth_header(self) -> str:
        return (
            f"Bearer {self._api_key}"
            if self._api_key.startswith("t1.")
            else f"Api-Key {self._api_key}"
        )

    def _model_uri(self) -> str:
        # Format: gpt://<folder_id>/<model_name>/latest
        # Accept user-supplied "yandexgpt-lite/latest" or full
        # "gpt://folder/model" URIs.
        if self._model.startswith("gpt://"):
            return self._model
        return f"gpt://{self._folder_id}/{self._model}"

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
        }
        payload = {
            "modelUri": self._model_uri(),
            "completionOptions": {
                "stream": False,
                "temperature": request.temperature,
                "maxTokens": str(request.max_tokens),
            },
            "messages": [
                {"role": "system", "text": request.system},
                {"role": "user", "text": request.user},
            ],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(self._base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        result = data.get("result") or {}
        usage = result.get("usage") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("inputTextTokens"))
        self.last_output_tokens = _coerce_token_count(usage.get("completionTokens"))
        alternatives = result.get("alternatives") or []
        if not alternatives:
            return ""
        message = (alternatives[0] or {}).get("message") or {}
        return str(message.get("text", "")).strip()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Fallback to non-stream + yield as one chunk.

        YandexGPT does support streaming but emits a Yandex-specific
        frame format — implementing the parser is more work than it's
        worth for the volume Persona users hit. The wrapper falls back
        to ``complete()`` so the /ask streaming UI still renders.
        """
        text = await self.complete(request)
        if text:
            yield text


class GigaChatClient:
    """GigaChat provider (Sber) — works in Russia, free tier of 1M tokens/mo.

    GigaChat uses OAuth-2 ``client_credentials`` flow: the user gets a
    ``Client Secret`` from developers.sber.ru, and the client trades it
    for a short-lived access token on each request. We cache the token
    for 25 minutes (server says 30, leave 5 min safety margin) so we
    don't hammer the token endpoint.

    Defaults to ``GigaChat`` (the base/free model). Users can upgrade to
    ``GigaChat-Pro`` or ``GigaChat-Max`` in settings.

    The wire format is OpenAI-compatible chat.completions, so we can
    reuse :func:`_stream_openai_compatible` for streaming.
    """

    provider: Provider = "gigachat"

    _TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    _BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "GigaChat") -> None:
        # ``api_key`` here is GigaChat's ``Authorization`` key (base64
        # of client_id:client_secret per Sber docs). The user copies it
        # directly from their dashboard — no manual encoding needed.
        self._auth_key = api_key
        self._model = model
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    async def _get_token(self) -> str:
        import time  # noqa: PLC0415 — keep stdlib import local to avoid module bloat
        import uuid as _uuid  # noqa: PLC0415

        if self._cached_token and time.time() < self._token_expires_at - 60.0:
            return self._cached_token
        headers = {
            "Authorization": f"Basic {self._auth_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(_uuid.uuid4()),
        }
        async with httpx.AsyncClient(
            timeout=20.0,
            verify=False,  # noqa: S501 — Sber uses a CA chain not in default bundles
        ) as client:
            response = await client.post(
                self._TOKEN_URL,
                headers=headers,
                data={"scope": "GIGACHAT_API_PERS"},
            )
            response.raise_for_status()
            data = response.json()
        token = str(data.get("access_token") or "")
        expires_at = int(data.get("expires_at") or 0)
        if not token:
            raise LLMNotConfigured("GigaChat token issuance failed")
        self._cached_token = token
        # ``expires_at`` is unix-ms per Sber convention; convert to seconds.
        self._token_expires_at = expires_at / 1000.0 if expires_at else (
            time.time() + 25 * 60.0
        )
        return token

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
        token = await self._get_token()
        payload = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60.0, verify=False) as client:  # noqa: S501
            response = await client.post(self._BASE_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("prompt_tokens"))
        self.last_output_tokens = _coerce_token_count(usage.get("completion_tokens"))
        choices = data.get("choices") or []
        if not choices:
            return ""
        return str((choices[0] or {}).get("message", {}).get("content", "")).strip()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        token = await self._get_token()
        async for chunk in _stream_openai_compatible(
            base_url=self._BASE_URL,
            api_key=token,
            model=self._model,
            request=request,
            inner=self,
        ):
            yield chunk


class DeepSeekClient:
    """DeepSeek provider — works in Russia, OpenAI-compatible API.

    The cheapest serious-quality option on the market — $0.14 / 1M input
    tokens for the base ``deepseek-chat``, $0.55 / 1M for ``deepseek-reasoner``.
    No geo-restrictions, accepts cards from any country.

    The wire format is byte-for-byte OpenAI-compatible so we just point
    the same client at a different URL.
    """

    provider: Provider = "deepseek"

    def __init__(self, api_key: str, model: str = "deepseek-chat") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.deepseek.com/v1/chat/completions"
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
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
        usage = data.get("usage") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("prompt_tokens"))
        self.last_output_tokens = _coerce_token_count(usage.get("completion_tokens"))
        choices = data.get("choices") or []
        if not choices:
            return ""
        return str((choices[0] or {}).get("message", {}).get("content", "")).strip()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        async for chunk in _stream_openai_compatible(
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            request=request,
            inner=self,
        ):
            yield chunk


class OllamaClient:
    """Local-first LLM via Ollama (https://ollama.com).

    Ollama runs an open-weight model on the user's own machine and
    exposes an HTTP API on ``localhost:11434``. There is no API key, no
    rate limit, no usage tracking by any third party. The model file
    lives on the user's disk.

    Wire format: Ollama ships an OpenAI-compatible endpoint at
    ``/v1/chat/completions`` so we reuse the same SSE parser as Groq /
    DeepSeek. No vendor-specific wrangling needed.

    Defaults:
        endpoint: ``http://localhost:11434``
        model:    ``qwen2.5:3b`` (3B params, Q4 quantized ~2 GB,
                                  fits in 4 GB VRAM, decent Russian)

    Other recommended models for low-end hardware:
        ``qwen2.5:1.5b`` — 1 GB, CPU-only friendly
        ``llama3.2:3b``  — similar size, English-leaning
        ``phi3:mini``    — 3.8B, very strong for size, ~2.5 GB
        ``saiga``        — Russian-tuned Llama, ~5 GB

    The ``api_key`` field in our switcher is repurposed here as the
    endpoint URL — the user pastes ``http://localhost:11434`` instead
    of a secret. When the field is empty we fall back to the default.
    """

    provider: Provider = "ollama"

    _DEFAULT_ENDPOINT = "http://localhost:11434"
    _DEFAULT_MODEL = "qwen2.5:3b"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        # ``api_key`` is overloaded to mean "endpoint" for this provider.
        # An empty string means "use default localhost".
        endpoint = (api_key or "").strip().rstrip("/") or self._DEFAULT_ENDPOINT
        # Защита: если значение не похоже на URL (напр. случайно затёкший
        # generic ``byo_api_key`` — API-ключ/токен другого провайдера), НЕ даём
        # httpx упасть с UnsupportedProtocol («Request URL is missing an
        # http(s) protocol»). Откатываемся на локальный дефолт → в худшем случае
        # чистая ошибка соединения, а не 500 с непонятным текстом у юзера.
        if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
            endpoint = self._DEFAULT_ENDPOINT
        # OpenAI-compatible path lives under /v1; the legacy /api path
        # is also available but uses a different JSON shape.
        self._base_url = f"{endpoint}/v1/chat/completions"
        self._endpoint = endpoint
        self._model = (model or "").strip() or self._DEFAULT_MODEL
        # No API key needed for local Ollama, but the OpenAI-compatible
        # endpoint still demands a Bearer header. Send a placeholder so
        # the request parses; Ollama ignores the value.
        self._fake_key = "ollama"
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    # T29 — context window. Ollama's OpenAI-compat endpoint can't set
    # num_ctx, so it defaulted to ~4096; once the system prompt + history
    # filled it the model had ZERO room left and returned 1-token replies.
    # We call the NATIVE /api/chat with an explicit num_ctx. Bumped to 16384
    # because long chats + system prompt + tools + skills grew past 8192 and
    # starved the answer again. A model ALWAYS has a finite window (can't be
    # truly infinite) — but a request never asks for more than num_ctx tokens
    # of output, and we reserve room so input can't eat the whole window.
    _NUM_CTX = 16384

    def _native_messages(self, request: CompletionRequest) -> list[dict[str, object]]:
        """Build /api/chat messages. Native endpoint takes images as raw
        base64 in ``images: [...]`` (not the OpenAI image_url shape)."""
        user_msg: dict[str, object] = {"role": "user", "content": request.user or ""}
        if request.image_data_url:
            # T22.4 — normalise WebP → PNG (Ollama vision rejects WebP), then
            # strip the ``data:...;base64,`` prefix to the bare base64.
            normalised = _normalise_image_for_ollama(request.image_data_url)
            user_msg["content"] = request.user or "describe this image"
            user_msg["images"] = [normalised.split(",", 1)[-1]]
        return [
            {"role": "system", "content": request.system},
            user_msg,
        ]

    # keep_alive: держать модель в памяти, чтобы её не выгружало через 5 мин
    # (иначе каждый брифинг/проактив платит +10с холодным стартом). Переопределяется
    # env PERSONA_OLLAMA_KEEP_ALIVE (напр. "30m", "-1" = вечно).
    _KEEP_ALIVE = os.environ.get("PERSONA_OLLAMA_KEEP_ALIVE", "30m")
    # Ступени num_ctx: меньше контекст → меньше KV-кэш → быстрее старт и меньше
    # OOM на слабом GPU. Берём минимальную ступень, в которую влезает промпт+ответ.
    _NUM_CTX_STEPS = (4096, 8192, 16384)

    def _num_ctx_for(self, request: CompletionRequest) -> int:
        # Грубая оценка токенов промпта (RU/EN mixed ~3 символа/токен) + бюджет
        # ответа + запас. Картинка добавляет существенный расход — берём максимум.
        if request.image_data_url:
            return self._NUM_CTX
        chars = len(request.system or "") + len(request.user or "")
        needed = chars // 3 + int(request.max_tokens or 0) + 512
        for step in self._NUM_CTX_STEPS:
            if needed <= step:
                return step
        return self._NUM_CTX

    def _native_options(self, request: CompletionRequest) -> dict[str, object]:
        # num_predict = output cap. -1 would be unlimited; we keep the
        # caller's max_tokens but with num_ctx large enough that it isn't
        # starved by the prompt. Together this removes the "1 token" cap.
        return {
            "num_ctx": self._num_ctx_for(request),
            "num_predict": request.max_tokens,
            "temperature": request.temperature,
        }

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
        payload = {
            "model": self._model,
            "messages": self._native_messages(request),
            "stream": False,
            "keep_alive": self._KEEP_ALIVE,
            "options": self._native_options(request),
        }
        url = f"{self._endpoint}/api/chat"
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        self.last_input_tokens = _coerce_token_count(data.get("prompt_eval_count"))
        self.last_output_tokens = _coerce_token_count(data.get("eval_count"))
        return str((data.get("message") or {}).get("content", "")).strip()

    async def complete_json(
        self, request: CompletionRequest, schema: dict[str, object]
    ) -> dict[str, object]:
        """T29 — STRUCTURED output: constrain the model to ``schema`` via
        Ollama's ``format`` param and return the parsed object. This makes
        file generation reliable even on a weak 7B — the output is forced to
        valid JSON, so we never depend on the model "remembering" to call a
        tool correctly."""
        self.last_input_tokens = None
        self.last_output_tokens = None
        payload = {
            "model": self._model,
            "messages": self._native_messages(request),
            "stream": False,
            "format": schema,
            "keep_alive": self._KEEP_ALIVE,
            "options": self._native_options(request),
        }
        url = f"{self._endpoint}/api/chat"
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        self.last_input_tokens = _coerce_token_count(data.get("prompt_eval_count"))
        self.last_output_tokens = _coerce_token_count(data.get("eval_count"))
        content = str((data.get("message") or {}).get("content", "")).strip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("structured output was not a JSON object")
        return parsed

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.last_input_tokens = None
        self.last_output_tokens = None
        payload = {
            "model": self._model,
            "messages": self._native_messages(request),
            "stream": True,
            "keep_alive": self._KEEP_ALIVE,
            "options": self._native_options(request),
        }
        url = f"{self._endpoint}/api/chat"
        # read=600s — vision/cold-start models on 4GB VRAM take 60-120s for
        # the first token; the SSE keepalive upstream keeps the tunnel open.
        timeout = httpx.Timeout(600.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = (obj.get("message") or {}).get("content", "")
                    if chunk:
                        yield chunk
                    if obj.get("done"):
                        self.last_input_tokens = _coerce_token_count(
                            obj.get("prompt_eval_count")
                        )
                        self.last_output_tokens = _coerce_token_count(
                            obj.get("eval_count")
                        )


class WorkerLLMClient:
    """«Persona LLM Worker» — провайдер, считающий на ПК через очередь в БД.

    Архитектура убирает devtunnel: сервер НЕ ходит на ПК. Вместо этого
    ``stream()`` кладёт задачу ``kind='chat'`` в очередь (таблица ``llm_job``
    через :mod:`app.llm.worker_queue`), а ПК-агент (``ops/persona_llm_worker.py``)
    сам делает ИСХОДЯЩИЙ long-poll к серверу, забирает задачу, гоняет локальную
    Ollama и шлёт чанки/результат ОБРАТНО по HTTP. Здесь мы лишь поллим готовые
    чанки из БД и yield-им их в ТОМ ЖЕ формате, что :class:`OllamaClient.stream`
    — голые строки-дельты (новый кусок текста, не накопленный ответ), чтобы
    ``event_stream`` / ``_UsageRecordingClient`` работали без изменений.

    Все импорты ``worker_queue`` ленивые (внутри методов): модуль может ещё не
    приземлиться на момент сборки соседних слайсов, и провайдер не должен падать
    при импорте :mod:`app.llm.client`.
    """

    provider: Provider = "worker"

    #: Между опросами очереди — короткий сон, чтобы первые токены приходили
    #: почти сразу, но не жечь CPU тайтовым циклом.
    _POLL_INTERVAL = 0.04
    # Event-driven queue wakes immediately in the normal single-process
    # deployment. Полсекунды — только страховочный DB refresh для нескольких
    # uvicorn processes / process restart.
    _EVENT_FALLBACK_INTERVAL = 0.5
    #: Если за это время нет НИ одного нового чанка и задача не завершилась —
    #: считаем, что ПК-воркер завис/умер, и отдаём понятную ошибку.
    _STALL_TIMEOUT = 300.0

    def __init__(self, model: str | None = None, job_kind: str = "chat") -> None:
        # Модель, которую попросим посчитать на ПК. Берём ту же kv ``ollama_model``,
        # что и для прямого Ollama — на ПК крутится та же локальная модель.
        self._model = (model or "").strip() or OllamaClient._DEFAULT_MODEL
        clean_kind = re.sub(r"[^a-z0-9._-]", "_", str(job_kind or "").casefold())
        self._job_kind = clean_kind[:64] or "chat"
        # Совместимость с :class:`_UsageRecordingClient`: он читает эти поля после
        # стрима. Воркер токены не считает (Ollama на ПК), оставляем None.
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    def _messages(self, request: CompletionRequest) -> list[dict[str, object]]:
        """system+user → формат сообщений Ollama /api/chat (как у OllamaClient).

        Картинку, если есть, кладём как нативный Ollama-формат (raw base64 в
        ``images``) — ПК-агент передаёт payload.messages в /api/chat как есть.
        """
        user_msg: dict[str, object] = {"role": "user", "content": request.user or ""}
        if request.image_data_url:
            normalised = _normalise_image_for_ollama(request.image_data_url)
            user_msg["content"] = request.user or "describe this image"
            user_msg["images"] = [normalised.split(",", 1)[-1]]
        return [
            {"role": "system", "content": request.system},
            user_msg,
        ]

    async def complete(self, request: CompletionRequest) -> str:
        """Неблокирующий путь поверх :meth:`stream` — собрать дельты в строку.

        Часть кодовой базы зовёт ``complete`` (не ``stream``); реализуем его
        через стрим, чтобы не дублировать логику очереди.
        """
        chunks: list[str] = []
        async for delta in self.stream(request):
            chunks.append(delta)
        return "".join(chunks).strip()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.last_input_tokens = None
        self.last_output_tokens = None
        # Ленивые импорты: worker_queue (слайс W-A) может ещё не существовать
        # на момент сборки — тогда даём понятную ошибку, а не ImportError при
        # импорте модуля клиента.
        try:
            queue = importlib.import_module("app.llm.worker_queue")
            enqueue_job = queue.enqueue_job
            get_job = queue.get_job
            read_chunks = queue.read_chunks
            worker_online = queue.worker_online
        except Exception as exc:  # noqa: BLE001 — модуль очереди ещё не приземлился
            msg = (
                "ПК-воркер недоступен — модуль очереди не установлен "
                "(app.llm.worker_queue)."
            )
            raise LLMNotConfigured(msg) from exc

        if not await worker_online():
            msg = (
                "ПК-воркер офлайн — запусти persona_llm_worker на ПК "
                "(Ollama без туннеля)."
            )
            raise LLMNotConfigured(msg)

        # Опции считаем грубо как у OllamaClient — ПК-агент передаёт их в
        # /api/chat. num_ctx/num_predict защищают слабый GPU от старвейшна.
        options = {
            "num_predict": request.max_tokens,
            "temperature": request.temperature,
        }
        payload = {"messages": self._messages(request), "options": options}
        # user_id=0 — задача системная (без привязки к пользователю-владельцу).
        job_id = await enqueue_job(0, self._job_kind, self._model, payload)

        # ВАЖНО: агент шлёт первый чанк с seq=0, а read_chunks фильтрует seq>after_seq.
        # Стартуем с -1, иначе seq=0 (первый токен-батч) теряется — для чата это
        # съедало первый символ, а для JSON-вывода — открывающую ``` / '{', ломая
        # парсинг графа/фактов.
        last_seq = -1
        last_progress = _loop_time()
        read_job_update = getattr(queue, "read_job_update", None)
        wait_for_job_update = getattr(queue, "wait_for_job_update", None)
        forget_job_update = getattr(queue, "forget_job_update", None)
        terminal = False
        try:
            while True:
                if callable(read_job_update):
                    chunks, job = await read_job_update(job_id, last_seq)
                else:
                    # Backward-compatible path for older queue modules and
                    # light-weight test doubles.
                    chunks = await read_chunks(job_id, last_seq)
                    job = await get_job(job_id)
                for c in chunks:
                    # c — {seq, content}; seq монотонно растёт, content — дельта.
                    seq = int(c["seq"])
                    if seq > last_seq:
                        last_seq = seq
                    content = c.get("content")
                    if isinstance(content, str) and content:
                        yield content
                if chunks:
                    last_progress = _loop_time()

                status = (job or {}).get("status") if job else None
                if status == "done":
                    terminal = True
                    # Дочитываем хвост, появившийся между chunk/status SELECT.
                    tail = await read_chunks(job_id, last_seq)
                    for c in tail:
                        seq = int(c["seq"])
                        if seq > last_seq:
                            last_seq = seq
                        content = c.get("content")
                        if isinstance(content, str) and content:
                            yield content
                    return
                if status == "error":
                    terminal = True
                    err = (job or {}).get("error") or "ПК-воркер вернул ошибку"
                    raise LLMNotConfigured(f"ПК-воркер: {err}")

                # Сторож зависания: нет новых чанков и задача не финиширована.
                if _loop_time() - last_progress > self._STALL_TIMEOUT:
                    msg = (
                        "ПК-воркер не отвечает (таймаут ожидания токенов) — "
                        "проверь persona_llm_worker на ПК."
                    )
                    raise LLMNotConfigured(msg)
                if callable(wait_for_job_update):
                    await wait_for_job_update(
                        job_id, self._EVENT_FALLBACK_INTERVAL
                    )
                else:
                    await asyncio.sleep(self._POLL_INTERVAL)
        except BaseException:
            cancel_job = getattr(queue, "cancel_job", None)
            if not terminal and callable(cancel_job):
                try:
                    await asyncio.shield(
                        cancel_job(job_id, "request_cancelled_or_timed_out")
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "llm.worker.cancel_failed",
                        job_id=job_id,
                        error=type(exc).__name__,
                    )
            raise
        finally:
            if callable(forget_job_update):
                forget_job_update(job_id)

    async def complete_json(
        self, request: CompletionRequest, schema: dict[str, object]
    ) -> dict[str, object]:
        """Структурный вывод через ПК-воркер БЕЗ зависимости от GBNF/format.

        Промптим модель вернуть ТОЛЬКО JSON по схеме и парсим обычный стрим —
        так это работает с ЛЮБЫМ агентом, включая уже запущенный старый (не
        требует перезапуска воркера на ПК). Менее жёстко, чем format-constrained
        GBNF, но с temperature=0 + извлечением объекта {...} надёжно для граф-
        триплетов (knowledge_graph) и mem0-реконсиляции фактов (user_memory).
        """
        import json as _json  # noqa: PLC0415
        import re  # noqa: PLC0415

        schema_hint = _json.dumps(schema, ensure_ascii=False)
        sys_prompt = (request.system or "").rstrip()
        sys_prompt += (
            "\n\nВЕРНИ ТОЛЬКО валидный JSON строго по этой JSON-схеме, без "
            "markdown-обёрток и без пояснений. Схема: " + schema_hint
        )
        req2 = CompletionRequest(
            system=sys_prompt,
            user=request.user,
            max_tokens=request.max_tokens,
            temperature=0.0,
            image_data_url=request.image_data_url,
        )
        # self.complete → self.stream → обычная chat-задача (без format), которую
        # умеет уже запущенный агент. Собираем текст, достаём JSON.
        raw = (await self.complete(req2)).strip()

        if raw.startswith("```"):  # снять ```json ... ``` обёртку
            raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:  # вырезать объект {...}
            raw = raw[start : end + 1]
        try:
            parsed = _json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise LLMNotConfigured("не удалось распарсить JSON от ПК-воркера") from exc
        if not isinstance(parsed, dict):
            raise LLMNotConfigured("структурный вывод не является объектом JSON")
        return parsed


def _loop_time() -> float:
    """Монотонные секунды текущего loop (для таймаутов внутри стрима)."""
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        import time  # noqa: PLC0415

        return time.monotonic()


class _OpenAICompatibleClient:
    """Shared implementation for the dozen "OpenAI-compatible Bearer-token
    chat-completions" providers.

    The wire format is identical across OpenRouter, Mistral, Together,
    xAI, ProxyAPI, AITunnel, DeepSeek and several others — they all
    expose ``POST /v1/chat/completions`` with the standard
    ``{model, messages, temperature, max_tokens}`` body and SSE
    streaming. Subclassing this base keeps each per-vendor file to a
    one-line ``_base_url`` + ``_default_model`` override.

    Each subclass MUST set:
        provider     — the literal slug from :data:`Provider`
        _BASE_URL    — full ``/v1/chat/completions`` URL
        _DEFAULT_MODEL — slug accepted by that vendor (e.g. ``gpt-4o-mini``)

    Optional override:
        _OPTIONAL_HEADERS — dict added to every request (OpenRouter wants
                            ``HTTP-Referer`` + ``X-Title`` for analytics).
    """

    provider: Provider  # set by subclass
    _BASE_URL: str = ""
    _DEFAULT_MODEL: str = ""
    _OPTIONAL_HEADERS: dict[str, str] = {}

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = (model or "").strip() or self._DEFAULT_MODEL
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self._OPTIONAL_HEADERS)
        # T22.2 — vision attachment passthrough for OpenAI-compat providers.
        user_content: object = request.user
        if request.image_data_url:
            user_content = [
                {"type": "text", "text": request.user or "describe this image"},
                {
                    "type": "image_url",
                    "image_url": {"url": request.image_data_url},
                },
            ]
        payload = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": user_content},
            ],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(self._BASE_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("prompt_tokens"))
        self.last_output_tokens = _coerce_token_count(usage.get("completion_tokens"))
        choices = data.get("choices") or []
        if not choices:
            return ""
        return str((choices[0] or {}).get("message", {}).get("content", "")).strip()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        async for chunk in _stream_openai_compatible(
            base_url=self._BASE_URL,
            api_key=self._api_key,
            model=self._model,
            request=request,
            inner=self,
        ):
            yield chunk


class OpenRouterClient(_OpenAICompatibleClient):
    """OpenRouter aggregator — 400+ models behind a single API key.

    https://openrouter.ai/keys → create key → paste. The user picks
    which underlying model in ``kv_settings.openrouter_model`` (or just
    leave the default ``meta-llama/llama-3.1-8b-instruct:free`` which
    is on the free tier).

    OpenRouter wants two extra headers for usage analytics in their
    dashboard:
        HTTP-Referer — surface the calling app's URL
        X-Title       — surface the calling app's name
    """

    provider: Provider = "openrouter"
    _BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    _DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct:free"
    _OPTIONAL_HEADERS = {
        "HTTP-Referer": "https://github.com/SwairIt/persona",
        "X-Title": "Persona",
    }


class MistralClient(_OpenAICompatibleClient):
    """Mistral AI — EU-based, free tier, Apache-2 open-weight models.

    https://console.mistral.ai/api-keys
    Default: ``mistral-small-latest`` (free tier).
    """

    provider: Provider = "mistral"
    _BASE_URL = "https://api.mistral.ai/v1/chat/completions"
    _DEFAULT_MODEL = "mistral-small-latest"


class TogetherClient(_OpenAICompatibleClient):
    """Together AI — wide open-weight catalogue, $25 free credit on signup.

    https://api.together.xyz/settings/api-keys
    Default: ``meta-llama/Llama-3.1-8B-Instruct-Turbo`` (small + fast).
    """

    provider: Provider = "together"
    _BASE_URL = "https://api.together.xyz/v1/chat/completions"
    _DEFAULT_MODEL = "meta-llama/Llama-3.1-8B-Instruct-Turbo"


class XAIClient(_OpenAICompatibleClient):
    """xAI Grok — OpenAI-compatible. https://x.ai/api.

    Defaults to ``grok-4`` (newest as of mid-2026; older slugs auto-
    redirect to current per xAI release notes).
    """

    provider: Provider = "xai"
    _BASE_URL = "https://api.x.ai/v1/chat/completions"
    _DEFAULT_MODEL = "grok-4"


class ProxyAPIClient(_OpenAICompatibleClient):
    """ProxyAPI.ru — Russian gateway to OpenAI/Anthropic/Gemini.

    https://proxyapi.ru → register → pay in RUB → API key. Works from
    Russia without VPN. Models accessible by their canonical names
    (``gpt-4o``, ``claude-3-5-sonnet-20241022``, etc.).
    """

    provider: Provider = "proxyapi"
    _BASE_URL = "https://api.proxyapi.ru/openai/v1/chat/completions"
    _DEFAULT_MODEL = "gpt-4o-mini"


class AITunnelClient(_OpenAICompatibleClient):
    """AITunnel.ru — alternative Russian aggregator. https://aitunnel.ru.

    Same model as ProxyAPI: pay in rubles, access the big foreign
    providers without VPN. Default model name is OpenAI-flavoured.
    """

    provider: Provider = "aitunnel"
    _BASE_URL = "https://api.aitunnel.ru/v1/chat/completions"
    _DEFAULT_MODEL = "gpt-4o-mini"


async def _parse_gemini_sse(
    response: httpx.Response, client: GeminiClient
) -> AsyncIterator[str]:
    """Parse a Gemini ``streamGenerateContent`` SSE stream into deltas."""
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        usage = event.get("usageMetadata") or {}
        if usage:
            client.last_input_tokens = _coerce_token_count(
                usage.get("promptTokenCount")
            )
            client.last_output_tokens = _coerce_token_count(
                usage.get("candidatesTokenCount")
            )
        candidates = event.get("candidates") or []
        if not candidates:
            continue
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text:
                yield text


def _normalise_image_for_ollama(data_url: str) -> str:
    """T22.4/22.9 — convert any image data URL to safe PNG, downscale if huge.

    Three things go wrong with raw browser uploads:
      * WebP / weird formats — Ollama vision rejects with 400.
      * Resolution > ~1500px — qwen2.5vl & co rejects with 400 even on
        PNG. Vision encoders have a hard pixel limit and don't auto-
        resize; we cap the longest side at 1280px which fits everyone.
      * RGBA / palette modes — same rejection family.

    Pillow handles all three: decode → resize → RGB → re-encode PNG.
    """
    if not data_url.startswith("data:"):
        return data_url
    try:
        header, b64 = data_url.split(",", 1)
    except ValueError:
        return data_url

    try:
        import base64  # noqa: PLC0415
        import io
        from PIL import Image

        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw))

        # T22.9 — downscale huge screenshots. 1280px keeps text legible
        # for vision models without busting their encoder limit.
        max_side = 1280
        if max(img.size) > max_side:
            scale = max_side / max(img.size)
            new_w = int(img.size[0] * scale)
            new_h = int(img.size[1] * scale)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        if img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=False)
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception:
        # If conversion fails, fall back to original — Ollama will then
        # surface its own error which is at least diagnosable.
        return data_url


def _coerce_token_count(value: object) -> int | None:
    """Normalise a provider-reported token count.

    Provider responses are user-controlled JSON and we deliberately
    don't trust them: the value may be missing, ``None``, a non-numeric
    string, or a negative number. Anything we can't honestly parse as a
    non-negative integer collapses to ``None`` so the ledger row records
    ``NULL`` rather than a misleading ``0`` or a fabricated guess.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass; collapse it to None so a
        # provider that erroneously sent ``true`` doesn't get recorded
        # as a 1-token call.
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            coerced = int(value)
        except ValueError:
            return None
        return coerced if coerced >= 0 else None
    return None


# ---------------------------------------------------------------------------
# kv + vault lookup helpers
# ---------------------------------------------------------------------------


async def _resolve_provider_and_key() -> tuple[str | None, str | None]:
    """Read the saved provider + key without ever logging the plaintext.

    Order of precedence:

    1. ``kv_settings.llm_provider`` (v0.71 switcher).
    2. ``kv_settings.byo_api_provider`` (v0.50 wizard).
    3. ``Settings.byo_api_provider`` (env / .env).
    4. Hard default ``anthropic``.

    For the key:

    1. Vault row ``llm_api_key_<provider>`` (probed via ``list_keys`` —
       the master password is **never** known here, so we cannot
       decrypt; encrypted keys are surfaced via the per-feature
       routes that *do* hold the password).
    2. ``kv_settings.byo_api_key_<provider>`` (v0.71 fallback).
    3. ``kv_settings.byo_api_key`` (v0.50 single-key wizard).
    4. ``Settings.byo_api_key`` (env / .env).

    Returns ``(provider, key)``. Either may be ``None`` if absent;
    callers raise :class:`LLMNotConfigured` from there.
    """
    # Local imports keep the module importable in environments where
    # the storage layer is not yet wired (unit tests of the clients
    # themselves don't need a DB).
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415
    from app.vault import list_keys  # noqa: PLC0415

    cfg = get_settings()

    # KV reads degrade gracefully when the DB hasn't been initialised
    # yet (unit tests of make_client without the `db` fixture). The
    # env / Settings fallbacks below are enough to satisfy the resolver
    # contract in that case.
    try:
        async with get_connection() as conn:
            kv_provider = await get_kv(conn, _KV_LLM_PROVIDER)
            legacy_provider = await get_kv(conn, _KV_LEGACY_PROVIDER)
    except Exception:
        kv_provider = None
        legacy_provider = None

    raw_provider = (kv_provider or legacy_provider or cfg.byo_api_provider or "").strip().lower()
    if raw_provider == "none":
        return ("none", None)
    if not raw_provider:
        raw_provider = "anthropic"

    # Vault probe — existence only. We don't have the master password
    # in this code path so we can't decrypt; the per-feature routes
    # that *do* hold the password should call set_secret / get_secret
    # directly and pass the key into make_client explicitly.
    try:
        vault_names = {row["key"] for row in await list_keys()}
    except Exception:
        vault_names = set()

    has_vault_row = _vault_key_for(raw_provider) in vault_names

    try:
        async with get_connection() as conn:
            kv_key_specific = await get_kv(conn, _kv_fallback_key_for(raw_provider))
            kv_key_legacy = await get_kv(conn, _KV_LEGACY_KEY)
    except Exception:
        kv_key_specific = None
        kv_key_legacy = None

    # Vault rows take precedence in *signalling* configured-ness but we
    # cannot return the plaintext without a password; in that case we
    # still fall through to whichever kv fallback exists so the running
    # process can call the API. This matches the v0.50 setup-wizard
    # contract where the vault is a strict upgrade over kv but kv is
    # the safety net.
    key = kv_key_specific or kv_key_legacy or cfg.byo_api_key or None
    if key is not None:
        key = key.strip() or None

    log.info(
        "llm.client.resolved",
        provider=raw_provider,
        key_present=key is not None,
        vault_row_present=has_vault_row,
        source=(
            "kv_specific" if kv_key_specific
            else "kv_legacy" if kv_key_legacy
            else "env" if cfg.byo_api_key
            else "missing"
        ),
    )

    return (raw_provider, key)


def _resolve_provider_and_key_sync() -> tuple[str | None, str | None]:
    """Synchronous wrapper for the async resolver.

    ``make_client`` is called from both sync and async paths (CLI vs.
    request handlers) so we briefly spin up an event loop when none
    is running. Inside a running loop we use ``asyncio.run_coroutine_
    threadsafe`` via a fresh thread to avoid the "this event loop is
    already running" error.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_resolve_provider_and_key())

    # We are inside a running loop — punt to a worker thread so the
    # nested asyncio.run() call is legal.
    import concurrent.futures  # noqa: PLC0415 — only needed on this branch

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, _resolve_provider_and_key())
        return future.result()


def _read_kv_sync(key: str) -> str | None:
    """One-shot kv read for provider-specific extras (Yandex folder_id, etc).

    Synchronous to match :func:`_resolve_provider_and_key_sync` so the
    same call site convention applies. Returns ``None`` on any failure
    instead of raising — these extras are advisory.
    """
    async def _go() -> str | None:
        try:
            from app.storage.db import get_connection  # noqa: PLC0415
            from app.storage.repository import get_kv  # noqa: PLC0415
            async with get_connection() as conn:
                value = await get_kv(conn, key)
        except Exception:
            return None
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_go())
    import concurrent.futures  # noqa: PLC0415
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _go()).result()


class _UsageRecordingClient:
    """Thin wrapper that persists a ``llm_usage`` row after every call.

    Wraps any concrete :class:`LLMClient` (Anthropic / OpenAI / Groq)
    and forwards :meth:`complete` to it. After the inner call returns
    — successful or not — a single row is written to ``llm_usage`` so
    ``/stats/llm-usage`` can render the per-day burn chart.

    The wrapper deliberately swallows ledger-write failures: if the
    SQLite write blows up for any reason (disk full, schema drift,
    concurrent migration) we structlog the error and let the original
    completion result propagate. Token bookkeeping must never break a
    user-facing feature.
    """

    def __init__(self, inner: LLMClient, kind: str) -> None:
        self._inner = inner
        self._kind = kind
        # Surface the provider through the protocol so callers that
        # introspect ``client.provider`` (e.g. /settings/llm health
        # check) see the same value they would have got from the bare
        # underlying client.
        self.provider: Provider = inner.provider

    def __getattr__(self, name: str) -> object:
        # Проксируем на inner возможности, которых нет у самой обёртки — прежде
        # всего ``complete_json`` (структурный GBNF-вывод для граф-триплетов и
        # mem0-реконсиляции). Без этого обёртка СКРЫВАЛА complete_json, и
        # ``hasattr(client, 'complete_json')`` был False → извлечение триплетов
        # графа всегда возвращало пусто (граф не строился ни через воркер, ни
        # напрямую). __getattr__ зовётся только для НЕнайденных атрибутов, так
        # что complete/stream/provider/_inner идут обычным путём и не проксируются.
        return getattr(object.__getattribute__(self, "_inner"), name)

    async def complete(self, request: CompletionRequest) -> str:
        try:
            text = await self._inner.complete(request)
        except Exception:
            await _record_usage(
                kind=self._kind,
                provider=self._inner.provider,
                input_tokens=None,
                output_tokens=None,
                success=False,
            )
            raise

        # ``last_input_tokens`` / ``last_output_tokens`` are the protocol
        # we just added to every concrete client. ``getattr`` with a
        # ``None`` default keeps the wrapper duck-typed — a future
        # third-party LLMClient that hasn't been updated still records a
        # row, just with NULL tokens.
        input_tokens = getattr(self._inner, "last_input_tokens", None)
        output_tokens = getattr(self._inner, "last_output_tokens", None)
        await _record_usage(
            kind=self._kind,
            provider=self._inner.provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            success=True,
        )
        return text

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Forward each streamed delta, then persist one usage row.

        Mirrors the contract of :meth:`complete`: ledger writes happen
        once at end-of-stream (success or failure) and never block the
        delta from reaching the caller. A streaming exception still
        records a ``success=False`` row before re-raising so the
        per-day burn chart counts failed streams.
        """
        try:
            inner_stream = self._inner.stream(request)
            async for delta in inner_stream:
                yield delta
        except Exception:
            await _record_usage(
                kind=self._kind,
                provider=self._inner.provider,
                input_tokens=None,
                output_tokens=None,
                success=False,
            )
            raise

        input_tokens = getattr(self._inner, "last_input_tokens", None)
        output_tokens = getattr(self._inner, "last_output_tokens", None)
        await _record_usage(
            kind=self._kind,
            provider=self._inner.provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            success=True,
        )


async def _record_usage(
    *,
    kind: str,
    provider: str,
    input_tokens: int | None,
    output_tokens: int | None,
    success: bool,
) -> None:
    """Append a single row to ``llm_usage``.

    Parametrised SQL — never string-interpolates the values — and the
    whole block is wrapped in ``try/except`` so a ledger-write failure
    can never poison the calling feature. Logs at INFO on success and
    WARNING on failure (without the raw exception's traceback in the
    log payload — structlog's ``exception`` adds it once).
    """
    try:
        from app.storage.db import get_connection  # noqa: PLC0415 — circular guard

        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO llm_usage (kind, provider, input_tokens, "
                "output_tokens, success) VALUES (?, ?, ?, ?, ?)",
                (kind, provider, input_tokens, output_tokens, 1 if success else 0),
            )
            await conn.commit()
    except Exception as exc:
        usage_log.warning(
            "llm_usage.record.failed",
            kind=kind,
            provider=provider,
            error=str(exc),
        )
        return

    usage_log.info(
        "llm_usage.recorded",
        kind=kind,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        success=success,
    )


def make_client(
    provider: Provider | None = None,
    api_key: str | None = None,
    kind: str = "unknown",
) -> LLMClient:
    """Construct a client from kv_settings (or explicit args).

    BYO key model: the API key NEVER touches the Persona backend in
    transit other than briefly in-memory during the request. We don't
    log it, persist it server-side outside the user's own vault / kv
    rows, or expose it via any endpoint.

    Resolution order when ``provider`` / ``api_key`` are not passed:

    * Provider comes from ``kv_settings.llm_provider`` (v0.71 switcher),
      falling back to ``kv_settings.byo_api_provider`` (v0.50 wizard),
      then ``Settings.byo_api_provider`` (env), then the hard default
      ``anthropic``.
    * Key is looked up *vault first* (existence probe — the decrypted
      value can only come back through the per-feature routes that
      hold the master password), then the per-provider kv fallback
      ``byo_api_key_<provider>``, then the legacy single-key row
      ``byo_api_key``, then ``Settings.byo_api_key``.

    The literal string ``"none"`` as a provider disables every AI
    feature — :class:`LLMNotConfigured` is raised so calling features
    short-circuit rather than 500.
    """
    use_provider: str | None
    use_key: str | None

    if provider is not None or api_key is not None:
        cfg = get_settings()
        use_provider = (provider or cfg.byo_api_provider or "").strip().lower() or None
        use_key = (api_key or cfg.byo_api_key or "").strip() or None
    else:
        use_provider, use_key = _resolve_provider_and_key_sync()

    if use_provider == "none":
        msg = (
            "LLM features disabled (provider=none). "
            "Switch provider at /settings/llm to re-enable."
        )
        raise LLMNotConfigured(msg)

    # T10 — Ollama is the one exception: it needs no API key because the
    # endpoint runs locally on the user's own machine. Empty key means
    # "use default http://localhost:11434".
    if not use_provider:
        msg = (
            "LLM not configured. Pick a provider + paste a key at "
            "/settings/llm, or set PERSONA_BYO_API_PROVIDER "
            "(anthropic|openai|groq|gemini|yandex|gigachat|deepseek|ollama) "
            "+ PERSONA_BYO_API_KEY in .env."
        )
        raise LLMNotConfigured(msg)
    # W-B — Ollama и worker не требуют API-ключа: Ollama локален, а worker
    # считает на ПК через очередь (ключ воркера живёт в kv, не в use_key).
    if not use_key and use_provider not in ("ollama", "worker"):
        msg = (
            "LLM not configured. Pick a provider + paste a key at "
            "/settings/llm, or set PERSONA_BYO_API_PROVIDER "
            "(anthropic|openai|groq|gemini|yandex|gigachat|deepseek) "
            "+ PERSONA_BYO_API_KEY in .env."
        )
        raise LLMNotConfigured(msg)

    inner: LLMClient
    if use_provider == "anthropic":
        inner = AnthropicClient(use_key)
    elif use_provider == "openai":
        inner = OpenAIClient(use_key)
    elif use_provider == "groq":
        inner = GroqClient(use_key)
    elif use_provider == "gemini":
        inner = GeminiClient(use_key)
    elif use_provider == "yandex":
        # YandexGPT needs an optional folder_id. We pull it from kv —
        # the /settings/llm page surfaces a separate text field for it
        # next to the api key when ``yandex`` is selected.
        folder_id = _read_kv_sync("yandex_folder_id") or None
        inner = YandexGPTClient(use_key, folder_id=folder_id)
    elif use_provider == "gigachat":
        inner = GigaChatClient(use_key)
    elif use_provider == "deepseek":
        inner = DeepSeekClient(use_key)
    elif use_provider == "ollama":
        # Ollama needs no API key — local server on user's machine.
        # The ``use_key`` here is repurposed as the endpoint URL.
        # An optional model override lives in kv ``ollama_model``.
        model_override = _read_kv_sync("ollama_model") or None
        inner = OllamaClient(use_key, model=model_override)
    elif use_provider == "worker":
        # W-B — «Persona LLM Worker». Ключа нет: задачи кладём в очередь, ПК
        # сам забирает их long-poll'ом. Модель — та же kv ``ollama_model``
        # (на ПК крутится локальная Ollama), иначе дефолт OllamaClient.
        model_override = _read_kv_sync("ollama_model") or None
        inner = WorkerLLMClient(model=model_override, job_kind=kind)
    elif use_provider == "openrouter":
        # OpenRouter — the user picks which underlying model in kv
        # ``openrouter_model``. If unset, fall back to the free Llama 3.1.
        inner = OpenRouterClient(use_key, model=_read_kv_sync("openrouter_model"))
    elif use_provider == "mistral":
        inner = MistralClient(use_key, model=_read_kv_sync("mistral_model"))
    elif use_provider == "together":
        inner = TogetherClient(use_key, model=_read_kv_sync("together_model"))
    elif use_provider == "xai":
        inner = XAIClient(use_key, model=_read_kv_sync("xai_model"))
    elif use_provider == "proxyapi":
        inner = ProxyAPIClient(use_key, model=_read_kv_sync("proxyapi_model"))
    elif use_provider == "aitunnel":
        inner = AITunnelClient(use_key, model=_read_kv_sync("aitunnel_model"))
    else:
        msg = f"Unsupported LLM provider: {use_provider}"
        raise LLMNotConfigured(msg)

    # Every concrete client is wrapped so /stats/llm-usage can read the
    # per-day burn chart no matter which feature triggered the call.
    # Existing callers that don't pass ``kind`` show up as ``"unknown"``
    # — better than dropping the row, since the chart total still adds
    # up to the operator's actual provider bill.
    return _UsageRecordingClient(inner, kind=kind)
