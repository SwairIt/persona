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
from app.auth.owner import is_owner
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


# Known-installed local Ollama models on the user's PC. Used for the WORKER
# provider (outbound long-poll — the server cannot reach the PC's /api/tags,
# so we can't enumerate live) and as a fallback for OLLAMA when its endpoint
# is unreachable. Overridable via kv ``worker_models`` (comma-separated) so
# the list stays editable without a code change / when new models are pulled.
_DEFAULT_WORKER_MODELS: tuple[str, ...] = (
    "gemma3:4b",
    "qwen2.5:3b",
    "qwen2.5:7b",
)


def _installed_models_struct(names: list[str]) -> list[dict[str, object]]:
    """Build picker model structs from a list of installed model names,
    attaching descriptions (from ``_OLLAMA_DESCRIPTIONS``) and a vision flag."""
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in names:
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        is_vision = any(
            kw in name.lower() for kw in ("vl", "vision", "llava", "moondream")
        )
        out.append({
            "name": name,
            "description": _OLLAMA_DESCRIPTIONS.get(name, ""),
            "vision": is_vision,
        })
    return out


async def _list_models_for_user(
    user_id: int,
    providers_tuple: tuple[tuple[str, str, str], ...],
) -> JSONResponse:
    """Пикер моделей для ОБЫЧНОГО пользователя — только его собственный конфиг.

    Раньше этот эндпоинт отдавал одно и то же всем авторизованным: активного
    провайдера ВЛАДЕЛЬЦА, живой список моделей с его Ollama-эндпоинта,
    курируемый список моделей его ПК-воркера и ``configured=True`` для
    ollama/worker. То есть чужой аккаунт видел железо владельца и мог выбрать
    его в пикере. Здесь всё считается из ``user_settings``:

    * провайдера ``worker`` в списке нет вообще (это домашний ПК владельца);
    * ``configured`` — только по СВОЕМУ ключу (для ollama — по своему URL);
    * список моделей Ollama тянем ТОЛЬКО с его эндпоинта, нет URL → пусто
      плюс подсказка, а не чужие модели.
    """
    from app.storage.repository import get_user_kv  # noqa: PLC0415

    async with get_connection() as conn:
        current_provider = (
            await get_user_kv(conn, user_id, "llm_provider") or "none"
        ).strip().lower()
        own_ollama = (
            await get_user_kv(conn, user_id, "byo_api_key_ollama") or ""
        ).strip()
        provider_keys: dict[str, bool] = {}
        current_models: dict[str, str | None] = {}
        for slug, _label, _placeholder in providers_tuple:
            if slug == "worker":
                continue
            key = (await get_user_kv(conn, user_id, f"byo_api_key_{slug}") or "").strip()
            # Для ollama «ключ» — это URL, и он ОБЯЗАТЕЛЕН: дефолта на
            # localhost для чужого аккаунта нет (это машина сервера).
            provider_keys[slug] = (
                bool(key) and (key.startswith("http://") or key.startswith("https://"))
                if slug == "ollama"
                else bool(key)
            )
            model_kv = "ollama_model" if slug == "ollama" else f"{slug}_model"
            current_models[slug] = await get_user_kv(conn, user_id, model_kv) or None

    providers_out: list[dict[str, object]] = []
    for slug, label, _placeholder in providers_tuple:
        if slug == "worker":
            continue
        models_struct: list[dict[str, object]]
        hint: str | None = None
        if slug == "ollama":
            models_struct = (
                await _list_ollama_models(own_ollama) if own_ollama else []
            )
            for m in models_struct:
                m["description"] = _OLLAMA_DESCRIPTIONS.get(str(m.get("name", "")), "")
            if not models_struct:
                hint = (
                    "Укажи URL своего Ollama на /settings/llm "
                    "(например http://192.168.1.10:11434)"
                    if not own_ollama
                    else "Твой Ollama не отвечает — проверь, что он запущен и доступен"
                )
        else:
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
        entry: dict[str, object] = {
            "slug": slug,
            "label": label,
            "configured": provider_keys.get(slug, False),
            "models": models_struct,
            "current_model": current_models.get(slug),
        }
        if hint:
            entry["hint"] = hint
        providers_out.append(entry)

    return JSONResponse({
        "providers": providers_out,
        "current": {
            "provider": current_provider,
            "model": current_models.get(current_provider),
        },
    })


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

    if not await is_owner(int(session["user_id"])):
        return await _list_models_for_user(int(session["user_id"]), PROVIDERS_TUPLE)

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
            # Ollama и worker (ПК-воркер) не требуют ключа — они локальные.
            provider_keys[slug] = bool(key.strip()) or slug in ("ollama", "worker")

        # Список локально установленных моделей (kv worker_models, через
        # запятую) с фолбэком на дефолт. Общий для ollama и worker.
        worker_models_raw = (await get_kv(conn, "worker_models") or "").strip()
        installed_names = (
            [n for n in worker_models_raw.replace(";", ",").split(",") if n.strip()]
            if worker_models_raw
            else list(_DEFAULT_WORKER_MODELS)
        )

        # Current model per provider (kv-stored if user explicitly set).
        # ollama и worker делят одну kv ``ollama_model`` (её читает
        # WorkerLLMClient/OllamaClient), поэтому для них берём именно её.
        current_models: dict[str, str | None] = {}
        for slug, _label, _placeholder in PROVIDERS_TUPLE:
            model_kv = "ollama_model" if slug in ("ollama", "worker") else f"{slug}_model"
            current_models[slug] = await get_kv(conn, model_kv) or None

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
            if not ollama_models:
                # Endpoint недоступен (напр. работаем через outbound-воркер,
                # LAN-Ollama на сервере нет) — показываем известный список
                # установленных моделей, чтобы пикер не был пустым.
                ollama_models = _installed_models_struct(installed_names)
            models_struct = ollama_models
            configured = True
        elif slug == "worker":
            # ПК-воркер ходит outbound long-poll'ом — сервер НЕ может дёрнуть
            # /api/tags на ПК, поэтому отдаём курируемый список установленных
            # моделей. Ключ не нужен (токен воркера — отдельно).
            models_struct = _installed_models_struct(installed_names)
            configured = True
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
