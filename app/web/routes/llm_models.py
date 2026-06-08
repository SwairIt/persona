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


# T24 — добавлены описания для каждой модели (показываются в picker'е).
# Format: ``{provider: [(name, description), ...]}``
_PROVIDER_DEFAULTS: dict[str, list[tuple[str, str]]] = {
    "anthropic": [
        ("claude-haiku-4-5", "быстрая, дешёвая, хороша для частых задач"),
        ("claude-sonnet-4-6", "баланс скорость/качество, дефолт для большинства"),
        ("claude-opus-4-7", "максимум интеллекта, медленнее и дороже"),
    ],
    "openai": [
        ("gpt-4o-mini", "дешёвая, быстрая, видит картинки"),
        ("gpt-4o", "флагман OpenAI, multimodal"),
        ("o1-mini", "reasoning модель — медленно но качественно для логики"),
    ],
    "groq": [
        ("llama-3.3-70b-versatile", "Llama 70B на ультра-быстром инференсе Groq"),
        ("llama-3.1-8b-instant", "Llama 8B, мгновенный ответ"),
    ],
    "gemini": [
        ("gemini-2.0-flash", "быстрая, бесплатная (1500 req/день), видит картинки"),
        ("gemini-1.5-pro", "более качественная, 1M контекста"),
    ],
    "yandex": [
        ("yandexgpt-lite/latest", "дешёвая лайт-версия от Яндекса"),
        ("yandexgpt/latest", "полная версия, качественный русский"),
    ],
    "gigachat": [
        ("GigaChat", "free tier 1М токенов/мес, базовая"),
        ("GigaChat-Pro", "лучше качество, платно"),
        ("GigaChat-Max", "флагман Сбера"),
    ],
    "deepseek": [
        ("deepseek-chat", "дешёвый и хороший в общем, $0.14/1M"),
        ("deepseek-reasoner", "reasoning модель — показывает мысли, медленнее"),
    ],
    "openrouter": [
        ("meta-llama/llama-3.1-8b-instruct:free", "бесплатная Llama 8B на OpenRouter"),
        ("anthropic/claude-3.5-sonnet", "Claude через OpenRouter"),
        ("openai/gpt-4o", "GPT-4o через OpenRouter"),
    ],
    "mistral": [
        ("mistral-small-latest", "small — дёшево и быстро, EU"),
        ("mistral-large-latest", "флагман Mistral"),
    ],
    "together": [
        ("meta-llama/Llama-3.1-8B-Instruct-Turbo", "Llama 8B на Together"),
        ("meta-llama/Llama-3.1-70B-Instruct-Turbo", "Llama 70B — мощно"),
    ],
    "xai": [
        ("grok-4", "флагман Илона, multimodal"),
        ("grok-3", "старая версия"),
    ],
    "proxyapi": [
        ("gpt-4o-mini", "через РФ-шлюз ProxyAPI"),
        ("gpt-4o", "GPT-4o через РФ-шлюз"),
        ("claude-3-5-sonnet-20241022", "Claude 3.5 через ProxyAPI"),
    ],
    "aitunnel": [
        ("gpt-4o-mini", "через РФ-шлюз AITunnel"),
        ("gpt-4o", "GPT-4o через AITunnel"),
    ],
}

# T24 — Ollama installed-model description map. Lookup by model name.
_OLLAMA_DESCRIPTIONS: dict[str, str] = {
    "qwen2.5:1.5b": "крошечная, быстрая, для простых задач",
    "qwen2.5:3b": "малая, быстрая, базовая",
    "qwen2.5:7b": "топ по тексту/коду для твоего железа, без vision",
    "qwen2.5vl:3b": "vision + русский, средний баланс",
    "qwen2.5vl:7b": "топ vision на 1050 Ti, медленно (~60с)",
    "llama3.2:1b": "крошечная Llama, мгновенно",
    "llama3.2:3b": "малая Llama",
    "llama3.2:8b": "Llama 8B",
    "moondream:latest": "крошечный vision (1B), быстрый, английский",
    "moondream:1.8b": "vision 1.8B, быстро",
    "llava:7b": "vision Llama-based",
    "llava:13b": "vision 13B, мощно но тяжко",
    "phi3:mini": "Microsoft Phi-3 mini (3.8B), сильный для размера",
    "gemma3:4b": "Google Gemma 3, multimodal (в Ollama пока без vision)",
    "deepseek-r1:1.5b": "тот reasoning модель малая",
    "deepseek-r1:7b": "reasoning модель — показывает мысли",
    "mistral:7b": "Mistral 7B, баланс",
    "saiga:8b": "Llama дообученная на русском",
}


async def _list_ollama_models(endpoint: str) -> list[dict[str, object]]:
    """Hit ``/api/tags`` on the Ollama endpoint and return installed models with capabilities.

    Returns list of ``{name, vision: bool}`` so the picker can show a 👁
    badge next to vision-capable models. Detection: Ollama's tag detail
    includes a ``capabilities`` array — vision/multimodal models include
    ``vision`` there.
    """
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
    out: list[dict[str, object]] = []
    for m in data.get("models", []):
        name = str(m.get("name", "")).strip()
        if not name:
            continue
        caps = m.get("capabilities") or []
        if isinstance(caps, list):
            caps_lower = [str(c).lower() for c in caps]
        else:
            caps_lower = []
        # Also heuristic: model names with 'vl', 'vision', 'llava',
        # 'moondream' = vision-capable. Ollama's capability list is
        # the source of truth when present, fallback to name match.
        is_vision = (
            "vision" in caps_lower
            or "vl" in name.lower()
            or "vision" in name.lower()
            or "llava" in name.lower()
            or "moondream" in name.lower()
        )
        out.append({"name": name, "vision": is_vision})
    return out


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
        models_struct: list[dict[str, object]]
        if slug == "ollama":
            endpoint = ollama_endpoint or "http://localhost:11434"
            ollama_models = await _list_ollama_models(endpoint)
            # T24 — attach descriptions to Ollama installed models
            for m in ollama_models:
                m["description"] = _OLLAMA_DESCRIPTIONS.get(
                    str(m.get("name", "")), ""
                )
            models_struct = ollama_models
            configured = bool(ollama_models)
        else:
            # T24 — cloud provider defaults now ship with descriptions.
            defaults = _PROVIDER_DEFAULTS.get(slug, [])
            models_struct = [
                {
                    "name": name,
                    "description": desc,
                    "vision": any(kw in name.lower() for kw in (
                        "vision", "vl", "4o", "claude", "gemini",
                        "sonnet", "opus", "grok", "haiku",
                    )),
                }
                for name, desc in defaults
            ]
            configured = provider_keys.get(slug, False)
        providers_out.append({
            "slug": slug,
            "label": label,
            "configured": configured,
            "models": models_struct,
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
