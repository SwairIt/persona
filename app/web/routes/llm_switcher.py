"""LLM provider switcher — choose Anthropic / OpenAI / Groq / none (v0.71).

The setup wizard at ``/setup`` writes the provider + key once at first
boot, but switching provider afterwards used to require either editing
``.env`` and restarting the process or hand-editing ``kv_settings``.
This module exposes a focused settings page at ``GET /settings/llm``
that does both jobs:

* Pick one of four radios — ``anthropic``, ``openai``, ``groq`` or
  ``none`` (the latter disables every AI feature without wiping any
  stored key).
* Paste a per-provider API key into the matching field. Each provider
  has its own input + vault row so rotating one credential never
  touches the others, and switching back to a previously-configured
  provider keeps working without re-entering the key.

Persistence rules mirror the setup wizard (:mod:`app.web.routes.setup`):

* The provider name is **not** secret — always a plain ``kv_settings``
  row under ``llm_provider`` (and mirrored to the legacy
  ``byo_api_provider`` row so :func:`app.llm.client.make_client`
  continues to see it on processes that still consult the v0.50 key).
* The API key goes into the v0.33 encrypted vault under
  ``llm_api_key_<provider>`` when ``cryptography`` is installed and the
  user supplied a master password. Otherwise it falls back to a plain
  ``kv_settings`` row (``byo_api_key_<provider>``). An empty key field
  is treated as "leave whatever is on file alone" so re-submitting the
  form to change provider only never wipes credentials.

The ``Test`` button POSTs to ``/settings/llm/test`` which builds a real
:class:`app.llm.client.LLMClient`, sends a one-token "ping" prompt and
reports either ``ok`` or the error string. The key never appears in any
log line, audit row or response body — :mod:`structlog` is given the
provider name, the configured-vs-missing flag and the HTTP status, and
absolutely nothing else.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.vault import get_secret, set_secret
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.llm.switcher")


# ---------------------------------------------------------------------------
# Provider catalogue + key naming
# ---------------------------------------------------------------------------

#: Tuples of ``(slug, label, placeholder)`` so the template iterates one
#: list rather than hard-coding three near-identical input blocks.
PROVIDERS: Final[tuple[tuple[str, str, str], ...]] = (
    # T9 (2026-06-07) — RU providers сверху, чтобы новые юзеры из России
    # видели первыми работающие у них варианты, а не GPT/Claude которые
    # требуют VPN + иностранную карту.
    ("yandex", "YandexGPT (через Yandex Cloud, работает в РФ)", "AQVN... или t1..."),
    ("gigachat", "GigaChat (Сбер, работает в РФ, 1М токенов/мес бесплатно)", "Authorization key из dashboard"),
    ("deepseek", "DeepSeek (работает в РФ, $0.14 за 1М токенов)", "sk-..."),
    ("anthropic", "Anthropic (Claude) — требует VPN из РФ", "sk-ant-..."),
    ("openai", "OpenAI (GPT-4o family) — требует VPN из РФ", "sk-..."),
    ("groq", "Groq (Llama 3 family) — работает в РФ", "gsk_..."),
    ("gemini", "Google Gemini — требует VPN из РФ", "AIza..."),
)

#: ``none`` disables AI features without deleting any stored key.
_VALID_PROVIDERS: Final[frozenset[str]] = frozenset(
    {slug for slug, _label, _placeholder in PROVIDERS} | {"none"},
)

#: New kv row written by this page. Kept distinct from the legacy
#: ``byo_api_provider`` so we can tell v0.71-saved values apart from
#: wizard-saved ones during debugging.
KV_LLM_PROVIDER: Final[str] = "llm_provider"
#: Legacy row consulted by ``make_client`` on older builds — we mirror
#: writes here so the running process picks up the change without a
#: rebuild.
KV_LEGACY_PROVIDER: Final[str] = "byo_api_provider"


def _vault_key_for(provider: str) -> str:
    """Vault row name for ``provider``'s API key (e.g. ``llm_api_key_openai``)."""
    return f"llm_api_key_{provider}"


def _kv_fallback_key_for(provider: str) -> str:
    """kv_settings row name used when the vault is unavailable."""
    return f"byo_api_key_{provider}"


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def _current_provider() -> str:
    """Read the saved provider, defaulting to ``anthropic``.

    Reads :data:`KV_LLM_PROVIDER` first, then the legacy
    ``byo_api_provider`` row so users upgrading from v0.50 land on the
    page already pointing at whichever provider the wizard configured.
    """
    async with get_connection() as conn:
        new = await get_kv(conn, KV_LLM_PROVIDER)
        legacy = await get_kv(conn, KV_LEGACY_PROVIDER)
    candidate = (new or legacy or "anthropic").strip().lower()
    if candidate not in _VALID_PROVIDERS:
        return "anthropic"
    return candidate


async def _key_status_per_provider(
    master_password: str | None,
) -> dict[str, dict[str, str]]:
    """Return ``{provider: {"configured": bool, "source": str}}`` for the UI.

    The plaintext value is *only* surfaced when the caller passed a
    valid master password — that lets the page pre-fill the input on
    re-render after a successful save without ever exposing keys to a
    drive-by visitor. When no password is supplied we still report
    *that* a row exists so the user gets a "configured" indicator,
    just without the value.
    """
    result: dict[str, dict[str, str]] = {}
    async with get_connection() as conn:
        for slug, _label, _placeholder in PROVIDERS:
            kv_value = await get_kv(conn, _kv_fallback_key_for(slug))
            kv_configured = bool(kv_value)
            vault_configured = False
            vault_value: str | None = None

            if master_password:
                vault_result = await get_secret(_vault_key_for(slug), master_password)
                vault_configured = vault_result.get("status") == "ok"
                if vault_configured:
                    vault_value = str(vault_result.get("value", ""))
            else:
                # Cheap existence probe — we list keys instead of
                # decrypting so we never need the password.
                from app.vault import list_keys  # noqa: PLC0415 — keep import surface small

                names = {row["key"] for row in await list_keys()}
                vault_configured = _vault_key_for(slug) in names

            entry: dict[str, str] = {
                "configured": "true" if (vault_configured or kv_configured) else "false",
                "source": "vault" if vault_configured else ("kv" if kv_configured else ""),
            }
            # Only embed the plaintext when we just decrypted it for the
            # current request — pre-filling the input box on the same
            # round-trip means the user can edit without retyping.
            if vault_value is not None:
                entry["value"] = vault_value
            elif vault_configured is False and kv_configured and master_password:
                entry["value"] = str(kv_value)
            result[slug] = entry
    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _persist_provider(provider: str) -> None:
    """Write the provider name to both the new and legacy kv rows."""
    async with get_connection() as conn:
        await set_kv(conn, KV_LLM_PROVIDER, provider)
        # Mirror to the legacy row so make_client picks the change up
        # on the same process; "none" is normalised to empty so the
        # client correctly raises LLMNotConfigured.
        await set_kv(
            conn,
            KV_LEGACY_PROVIDER,
            "" if provider == "none" else provider,
        )


async def _persist_key(
    provider: str,
    api_key: str,
    master_password: str,
) -> str:
    """Write ``api_key`` to the vault or kv fallback. Return persistence source.

    Empty ``api_key`` is treated as "leave alone" by the caller before
    we get here; this helper is only invoked for non-empty values.
    """
    if master_password:
        result = await set_secret(_vault_key_for(provider), api_key, master_password)
        if result.get("status") == "ok":
            return "vault"
    # Vault unavailable (missing cryptography) or no master password —
    # fall back to a plain kv row. We document the degradation in the
    # structured log; the audit trail records key-NAME only, never the
    # plaintext.
    async with get_connection() as conn:
        await set_kv(conn, _kv_fallback_key_for(provider), api_key)
    return "kv"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(
    request: Request,
    *,
    current_provider: str,
    keys: dict[str, dict[str, str]],
    notice: str | None = None,
    error: str | None = None,
    test_result: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Single render entry-point so every code path uses the same context."""
    return templates.TemplateResponse(
        request,
        "llm_switcher.html",
        {
            "title": "LLM provider",
            "active_nav": "settings",
            "providers": PROVIDERS,
            "current_provider": current_provider,
            "keys": keys,
            "notice": notice,
            "error": error,
            "test_result": test_result,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/settings/llm", response_class=HTMLResponse)
async def llm_switcher_page(request: Request) -> HTMLResponse:
    """Render the provider/key picker.

    No query parameters — the master password is never accepted via GET
    so it never lands in the access log or browser history.
    """
    current = await _current_provider()
    keys = await _key_status_per_provider(master_password=None)
    log.info(
        "llm.switcher.render",
        current_provider=current,
        configured=[slug for slug, info in keys.items() if info["configured"] == "true"],
    )
    return _render(request, current_provider=current, keys=keys)


@router.post("/settings/llm", response_class=HTMLResponse)
async def llm_switcher_save(
    request: Request,
    provider: str = Form(...),
    master_password: str = Form(""),
    anthropic_api_key: str = Form(""),
    openai_api_key: str = Form(""),
    groq_api_key: str = Form(""),
) -> HTMLResponse:
    """Persist the chosen provider and any newly-typed keys.

    Each provider's key field is independent: blank means "leave alone",
    non-blank means "rotate". The master password is only required for
    *vault* writes; without it (or without ``cryptography`` installed),
    keys land in a plain kv row instead.
    """
    chosen = provider.strip().lower()
    if chosen not in _VALID_PROVIDERS:
        current = await _current_provider()
        keys = await _key_status_per_provider(master_password=None)
        log.warning("llm.switcher.bad_provider", provider=chosen)
        await log_action(
            "llm.switcher.save",
            target=chosen,
            detail="bad provider",
            success=False,
        )
        return _render(
            request,
            current_provider=current,
            keys=keys,
            error=f"Unknown provider '{chosen}'.",
            status_code=400,
        )

    await _persist_provider(chosen)

    written: dict[str, str] = {}
    key_inputs = {
        "anthropic": anthropic_api_key,
        "openai": openai_api_key,
        "groq": groq_api_key,
    }
    for slug, raw_value in key_inputs.items():
        value = raw_value.strip()
        if not value:
            continue
        source = await _persist_key(slug, value, master_password)
        written[slug] = source

    keys = await _key_status_per_provider(master_password=master_password or None)

    await log_action(
        "llm.switcher.save",
        target=chosen,
        detail="written=" + ",".join(f"{p}:{src}" for p, src in written.items()),
        success=True,
    )
    log.info(
        "llm.switcher.saved",
        provider=chosen,
        keys_written={slug: src for slug, src in written.items()},
    )

    if chosen == "none":
        notice = "AI features disabled. Stored keys are untouched."
    elif written:
        notice = (
            f"Saved {chosen}. "
            f"Key stored in {written.get(chosen, '(unchanged)')}."
        )
    else:
        notice = f"Switched to {chosen}. Key on file is unchanged."

    return _render(
        request,
        current_provider=chosen,
        keys=keys,
        notice=notice,
    )


@router.post("/settings/llm/test", response_class=HTMLResponse)
async def llm_switcher_test(
    request: Request,
    master_password: str = Form(""),
) -> HTMLResponse:
    """Build a real client from the saved provider/key and send one prompt.

    The result string ("ok" or an error class + message — never the API
    key) is rendered back into the same page so the user gets immediate
    feedback without leaving ``/settings/llm``.
    """
    current = await _current_provider()
    keys = await _key_status_per_provider(master_password=master_password or None)

    if current == "none":
        return _render(
            request,
            current_provider=current,
            keys=keys,
            test_result="AI features are disabled (provider=none).",
        )

    try:
        client = make_client()
    except LLMNotConfigured as exc:
        log.info("llm.switcher.test.not_configured", provider=current)
        await log_action(
            "llm.switcher.test",
            target=current,
            detail="not_configured",
            success=False,
        )
        return _render(
            request,
            current_provider=current,
            keys=keys,
            test_result=f"Not configured: {exc}",
        )

    try:
        await client.complete(
            CompletionRequest(
                system="Reply with the single word: pong.",
                user="ping",
                max_tokens=4,
                temperature=0.0,
            )
        )
    except Exception as exc:
        log.warning(
            "llm.switcher.test.fail",
            provider=current,
            error_type=type(exc).__name__,
        )
        await log_action(
            "llm.switcher.test",
            target=current,
            detail=type(exc).__name__,
            success=False,
        )
        return _render(
            request,
            current_provider=current,
            keys=keys,
            test_result=f"Failed: {type(exc).__name__}",
        )

    log.info("llm.switcher.test.ok", provider=current)
    await log_action(
        "llm.switcher.test",
        target=current,
        detail="ok",
        success=True,
    )
    return _render(
        request,
        current_provider=current,
        keys=keys,
        test_result=f"OK — {current} responded.",
    )
