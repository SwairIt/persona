"""Bring-Your-Own-API-Key LLM client. We never see the user's key."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.logging_setup import get_logger
from app.settings import get_settings

Provider = Literal["anthropic", "openai", "groq"]

log = get_logger("persona.llm.switcher")

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

    async with get_connection() as conn:
        kv_provider = await get_kv(conn, _KV_LLM_PROVIDER)
        legacy_provider = await get_kv(conn, _KV_LEGACY_PROVIDER)

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

    async with get_connection() as conn:
        kv_key_specific = await get_kv(conn, _kv_fallback_key_for(raw_provider))
        kv_key_legacy = await get_kv(conn, _KV_LEGACY_KEY)

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


def make_client(
    provider: Provider | None = None,
    api_key: str | None = None,
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

    if not use_provider or not use_key:
        msg = (
            "LLM not configured. Pick a provider + paste a key at "
            "/settings/llm, or set PERSONA_BYO_API_PROVIDER "
            "(anthropic|openai|groq) + PERSONA_BYO_API_KEY in .env."
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
