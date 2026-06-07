"""T22 (2026-06-08) — enumerate available LLM models for the chat picker.

The /chat header has a dropdown that lets the user switch model
per-conversation (like Claude does). This module powers it:

  * GET /api/llm/models — returns all configured providers with their
    currently-available models. For Ollama we hit the LAN endpoint and
    list installed tags; for other providers we return the default
    model plus any kv-overridden value.

The response is cheap to fetch and cached aggressively on the client
side, so the dropdown opens instantly.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv

router = APIRouter(tags=["llm_models"])
log = get_logger("persona.llm_models")


# Default models per provider — shown when no kv override exists.
_PROVIDER_DEFAULTS: dict[str, list[str]] = {
    "anthropic": [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
    ],
    "openai": ["gpt-4o-mini", "gpt-4o", "o1-mini"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "gemini": ["gemini-2.0-flash", "gemini-1.5-pro"],
    "yandex": ["yandexgpt-lite/latest", "yandexgpt/latest"],
    "gigachat": ["GigaChat", "GigaChat-Pro", "GigaChat-Max"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "openrouter": [
        "meta-llama/llama-3.1-8b-instruct:free",
        "anthropic/claude-3.5-sonnet",
        "openai/gpt-4o",
    ],
    "mistral": ["mistral-small-latest", "mistral-large-latest"],
    "together": [
        "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        "meta-llama/Llama-3.1-70B-Instruct-Turbo",
    ],
    "xai": ["grok-4", "grok-3"],
    "proxyapi": ["gpt-4o-mini", "gpt-4o", "claude-3-5-sonnet-20241022"],
    "aitunnel": ["gpt-4o-mini", "gpt-4o"],
}


async def _list_ollama_models(endpoint: str) -> list[str]:
    """Hit ``/api/tags`` on the Ollama endpoint and return installed model names."""
    url = endpoint.rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return []
            data = response.json()
    except Exception as exc:
        log.debug("ollama.list.failed", endpoint=endpoint, error=str(exc))
        return []
    models = data.get("models", [])
    return [str(m.get("name", "")) for m in models if m.get("name")]


@router.get("/api/llm/models", response_class=JSONResponse)
async def list_models(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Return ``{providers: [{slug, label, models, configured}], current: {provider, model}}``.

    ``configured`` is True when the provider has a saved API key (or for
    Ollama, when we successfully hit the endpoint). The UI uses that to
    grey-out unreachable providers in the dropdown.
    """
    from app.web.routes.llm_switcher import PROVIDERS as PROVIDERS_TUPLE  # noqa: PLC0415

    async with get_connection() as conn:
        current_provider = (
            await get_kv(conn, "llm_provider")
            or await get_kv(conn, "byo_api_provider")
            or "none"
        ).strip().lower()
        # Per-provider current model overrides (kv) — populated by future
        # session.model writes. For now we read these directly.
        ollama_endpoint = (await get_kv(conn, "byo_api_key_ollama") or "").strip()
        provider_keys: dict[str, bool] = {}
        for slug, _label, _placeholder in PROVIDERS_TUPLE:
            key = await get_kv(conn, f"byo_api_key_{slug}") or ""
            provider_keys[slug] = bool(key.strip()) or slug == "ollama"

        # Current model per provider (kv-stored if user explicitly set).
        current_models: dict[str, str | None] = {}
        for slug, _label, _placeholder in PROVIDERS_TUPLE:
            current_models[slug] = await get_kv(conn, f"{slug}_model") or None

    # Build response array.
    providers_out: list[dict[str, object]] = []
    for slug, label, _placeholder in PROVIDERS_TUPLE:
        if slug == "ollama":
            # Live-fetch installed tags from user's Ollama.
            endpoint = ollama_endpoint or "http://localhost:11434"
            models = await _list_ollama_models(endpoint)
            configured = bool(models)
        else:
            models = list(_PROVIDER_DEFAULTS.get(slug, []))
            configured = provider_keys.get(slug, False)
        providers_out.append({
            "slug": slug,
            "label": label,
            "configured": configured,
            "models": models,
            "current_model": current_models.get(slug),
        })

    current_model = current_models.get(current_provider)
    return JSONResponse({
        "providers": providers_out,
        "current": {
            "provider": current_provider,
            "model": current_model,
        },
    })
