"""Bring-Your-Own-API-Key LLM client. We never see the user's key."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.logging_setup import get_logger
from app.settings import get_settings

Provider = Literal["anthropic", "openai", "groq", "gemini"]

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


class LLMClient(Protocol):
    provider: Provider

    async def complete(self, request: CompletionRequest) -> str: ...


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

    async def complete(self, request: CompletionRequest) -> str:
        self.last_input_tokens = None
        self.last_output_tokens = None
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

        usage = data.get("usage") or {}
        self.last_input_tokens = _coerce_token_count(usage.get("input_tokens"))
        self.last_output_tokens = _coerce_token_count(usage.get("output_tokens"))

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

    if not use_provider or not use_key:
        msg = (
            "LLM not configured. Pick a provider + paste a key at "
            "/settings/llm, or set PERSONA_BYO_API_PROVIDER "
            "(anthropic|openai|groq) + PERSONA_BYO_API_KEY in .env."
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
    else:
        msg = f"Unsupported LLM provider: {use_provider}"
        raise LLMNotConfigured(msg)

    # Every concrete client is wrapped so /stats/llm-usage can read the
    # per-day burn chart no matter which feature triggered the call.
    # Existing callers that don't pass ``kind`` show up as ``"unknown"``
    # — better than dropping the row, since the chart total still adds
    # up to the operator's actual provider bill.
    return _UsageRecordingClient(inner, kind=kind)
