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

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.llm.providers import (
    PRESETS,
    PRESETS_BY_SLUG,
    UNIVERSAL_SLUG,
    InvalidBaseURL,
    validate_base_url,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.vault import get_secret, set_secret
from app.web.templates_engine import templates

router = APIRouter(
    tags=["settings"],
    dependencies=[Depends(current_user_required)],
)
log = get_logger("persona.llm.switcher")


# ---------------------------------------------------------------------------
# Provider catalogue + key naming
# ---------------------------------------------------------------------------

#: Tuples of ``(slug, label, placeholder)`` so the template iterates one
#: list rather than hard-coding three near-identical input blocks.
PROVIDERS: Final[tuple[tuple[str, str, str], ...]] = (
    # === Бесплатно на твоём ПК ===
    ("ollama", "🏠 Локально через Ollama (бесплатно навсегда, твой ПК)", "http://localhost:11434 (или пусто для дефолта)"),
    # W-D: «обратный» воркер — ПК сам ходит на сервер за задачами (без туннеля).
    # Ключ-поле не используется (авторизация через токен воркера, см. кнопку ниже),
    # но провайдер должен быть в списке, чтобы его можно было выбрать радио-кнопкой.
    ("worker", "🖥️ Persona LLM Worker — твой ПК (без туннеля)", "ключ не нужен — токен генерируется кнопкой ниже"),

    # === Агрегаторы (один ключ → много моделей) ===
    # T12 (2026-06-07) — агрегаторы это лучший компромисс между
    # 'много моделей' и 'один логин': регистрируешься в одном месте,
    # получаешь один ключ, дальше переключаешься между моделями
    # выбором model name в kv-настройках.
    ("openrouter", "🌐 OpenRouter — 400+ моделей за один ключ (международный)", "sk-or-..."),
    ("proxyapi", "🇷🇺 ProxyAPI.ru — OpenAI/Claude/Gemini через РФ-шлюз в рублях", "sk-..."),
    ("aitunnel", "🇷🇺 AITunnel.ru — альтернативный РФ-агрегатор", "ключ из панели"),

    # === Российские прямые провайдеры ===
    ("yandex", "YandexGPT (через Yandex Cloud, работает в РФ)", "AQVN... или t1..."),
    ("gigachat", "GigaChat (Сбер, 1М токенов/мес бесплатно)", "Authorization key из dashboard"),

    # === Дешёвые иностранные ===
    ("deepseek", "DeepSeek (работает в РФ, $0.14 за 1М токенов)", "sk-..."),
    ("groq", "Groq (Llama family, работает в РФ, бесплатный tier)", "gsk_..."),
    ("mistral", "Mistral AI (EU, бесплатный tier)", "ключ с console.mistral.ai"),
    ("together", "Together AI ($25 кредитов на регистрации, open-weight)", "ключ с together.xyz"),

    # === Требуют VPN из РФ ===
    ("anthropic", "Anthropic (Claude) — VPN из РФ", "sk-ant-..."),
    ("openai", "OpenAI (GPT-4o family) — VPN из РФ", "sk-..."),
    ("gemini", "Google Gemini — VPN из РФ", "AIza..."),
    ("xai", "xAI Grok — VPN из РФ", "ключ с x.ai/api"),

    # === Универсальный: любой сервис с OpenAI-совместимым API ===
    # Единственный пункт, который не устаревает. Вписываешь адрес эндпоинта,
    # имя модели и ключ — работает ЛЮБОЙ сервис, говорящий протоколом
    # /chat/completions, включая те, которых ещё не существует.
    (
        UNIVERSAL_SLUG,
        "🔧 Свой OpenAI-совместимый сервис (адрес + модель + ключ)",
        "ключ сервиса",
    ),

    # === Пресеты: тот же протокол, отличается только базовый URL ===
    # Данные лежат в app/llm/providers.py — здесь просто разворачиваем их в
    # тот же кортеж, чтобы не держать две копии списка.
    *tuple((p.slug, p.label, p.placeholder) for p in PRESETS),
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
# Дополнительные поля OpenAI-совместимых сервисов (адрес эндпоинта + модель)
# ---------------------------------------------------------------------------
#
# У «обычного» провайдера ключ — единственное, что нужно ввести. У пресетов и
# у универсального ``openai_compatible`` полей три: ключ, адрес и модель.
# Адрес переопределяем У ВСЕХ пресетов намеренно (см. app/llm/providers.py):
# сервисы переезжают с домена на домен, и зашитая константа превращает переезд
# в баг, который чинится только релизом.

#: Слаги, у которых есть поля «свой URL» и «модель».
_URL_EDITABLE_SLUGS: Final[frozenset[str]] = (
    frozenset(PRESETS_BY_SLUG) | {UNIVERSAL_SLUG}
)


def _base_url_kv(slug: str) -> str:
    return f"{slug}_base_url"


def _model_kv(slug: str) -> str:
    return f"{slug}_model"


async def _extras_status(user_id: int | None) -> dict[str, dict[str, str]]:
    """Текущие адрес/модель + справка по каждому OpenAI-совместимому сервису.

    ``user_id is None`` — владелец (глобальный ``kv_settings``); иначе строки
    из ``user_settings`` конкретного участника. Ключи здесь НЕ читаются вовсе.
    """
    from app.storage.repository import get_user_kv  # noqa: PLC0415

    out: dict[str, dict[str, str]] = {}
    async with get_connection() as conn:
        for slug in sorted(_URL_EDITABLE_SLUGS):
            preset = PRESETS_BY_SLUG.get(slug)
            if user_id is None:
                base_url = await get_kv(conn, _base_url_kv(slug))
                model = await get_kv(conn, _model_kv(slug))
            else:
                base_url = await get_user_kv(conn, user_id, _base_url_kv(slug))
                model = await get_user_kv(conn, user_id, _model_kv(slug))
            out[slug] = {
                "base_url": (base_url or "").strip(),
                "model": (model or "").strip(),
                "default_base_url": preset.base_url if preset else "",
                "default_model": preset.default_model if preset else "",
                "key_hint": preset.key_hint if preset else (
                    "Ключ выдаёт сам сервис. Адрес пиши так, как он написан в "
                    "его доках (обычно .../v1) — хвост /chat/completions "
                    "допишется сам."
                ),
                "key_url": preset.key_url if preset else "",
                "confidence": preset.confidence if preset else "",
                "note": preset.note if preset else "",
                "required": "true" if preset is None else "false",
            }
    return out


def _collect_extras(
    form: dict[str, str], *, owner: bool
) -> tuple[dict[str, tuple[str | None, str | None]], str | None]:
    """Разобрать поля адреса/модели из формы. Второй элемент — текст ошибки.

    Пустое поле = «не трогать сохранённое» (та же семантика, что у ключей).
    Непустой адрес проверяется анти-SSRF правилом ПРЯМО ЗДЕСЬ, чтобы человек
    увидел понятную причину в форме, а не сетевую ошибку в чате через минуту.
    """
    parsed: dict[str, tuple[str | None, str | None]] = {}
    for slug in sorted(_URL_EDITABLE_SLUGS):
        raw_url = str(form.get(f"{slug}_base_url", "") or "").strip()
        raw_model = str(form.get(f"{slug}_model", "") or "").strip()
        if raw_url:
            try:
                raw_url = validate_base_url(raw_url, owner=owner)
            except InvalidBaseURL as exc:
                label = PRESETS_BY_SLUG[slug].label if slug in PRESETS_BY_SLUG else slug
                return ({}, f"Адрес для «{label}» отклонён: {exc}")
        parsed[slug] = (raw_url or None, raw_model or None)
    return (parsed, None)


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
    # Существование vault-строк спрашиваем ОДИН раз на всю страницу, а не по
    # разу на провайдера: список providers вырос втрое, и прежний вызов
    # ``list_keys()`` внутри цикла превратился бы в три десятка запросов на
    # каждую отрисовку /settings/llm.
    vault_names: set[str] = set()
    if not master_password:
        from app.vault import list_keys  # noqa: PLC0415 — keep import surface small

        vault_names = {row["key"] for row in await list_keys()}
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
                vault_configured = _vault_key_for(slug) in vault_names

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
# Per-user ветка: обычный пользователь настраивает СВОЙ LLM
# ---------------------------------------------------------------------------
#
# Владелец работает как раньше (kv_settings + vault + мастер-пароль). У всех
# остальных зарегистрированных аккаунтов свой провайдер и свой ключ в
# ``user_settings``: их запросы идут ИХ ключом, а «worker» (домашний ПК
# владельца) им недоступен в принципе.

#: Провайдеры, доступные не-владельцу — те же, что и в make_client, минус
#: worker. Импортируем из llm-слоя, чтобы список не разъезжался.
def _user_providers() -> tuple[tuple[str, str, str], ...]:
    from app.llm.client import _USER_ALLOWED_PROVIDERS  # noqa: PLC0415

    return tuple(
        (slug, label, placeholder)
        for slug, label, placeholder in PROVIDERS
        if slug in _USER_ALLOWED_PROVIDERS
    )


async def _user_current_provider(user_id: int) -> str:
    """Выбранный пользователем провайдер. Дефолта нет — «не настроено»."""
    from app.storage.repository import get_user_kv  # noqa: PLC0415

    async with get_connection() as conn:
        raw = await get_user_kv(conn, user_id, KV_LLM_PROVIDER)
    candidate = (raw or "").strip().lower()
    from app.llm.client import _USER_ALLOWED_PROVIDERS  # noqa: PLC0415

    if candidate == "none" or candidate in _USER_ALLOWED_PROVIDERS:
        return candidate
    return "none"


async def _user_key_status(user_id: int) -> dict[str, dict[str, str]]:
    """``{provider: {"configured": ..., "source": "user"}}`` по user_settings.

    Значения ключей НИКОГДА не попадают в результат — только факт наличия,
    как и в owner-ветке (см. ``safe_keys`` в :func:`_render`).
    """
    from app.storage.repository import get_user_kv  # noqa: PLC0415

    result: dict[str, dict[str, str]] = {}
    async with get_connection() as conn:
        for slug, _label, _placeholder in _user_providers():
            raw = (await get_user_kv(conn, user_id, _kv_fallback_key_for(slug)) or "").strip()
            result[slug] = {
                "configured": "true" if raw else "false",
                "source": "user" if raw else "",
            }
    return result


async def _persist_user_choice(
    user_id: int,
    provider: str,
    form: dict[str, str],
    extras: dict[str, tuple[str | None, str | None]] | None = None,
) -> dict[str, str]:
    """Сохранить провайдера + непустые ключи пользователя. Возвращает записанное."""
    from app.storage.repository import set_user_kv  # noqa: PLC0415
    from app.web.templates_engine import invalidate_user_kv_sync  # noqa: PLC0415

    written: dict[str, str] = {}
    async with get_connection() as conn:
        await set_user_kv(conn, user_id, KV_LLM_PROVIDER, provider)
        invalidate_user_kv_sync(user_id, KV_LLM_PROVIDER)
        for slug, _label, _placeholder in _user_providers():
            raw = str(form.get(f"{slug}_api_key", "") or "").strip()
            if not raw:
                continue
            await set_user_kv(conn, user_id, _kv_fallback_key_for(slug), raw)
            invalidate_user_kv_sync(user_id, _kv_fallback_key_for(slug))
            written[slug] = "user"
        for slug, (base_url, model) in (extras or {}).items():
            if base_url:
                await set_user_kv(conn, user_id, _base_url_kv(slug), base_url)
                invalidate_user_kv_sync(user_id, _base_url_kv(slug))
            if model:
                await set_user_kv(conn, user_id, _model_kv(slug), model)
                invalidate_user_kv_sync(user_id, _model_kv(slug))
    return written


def _user_save_notice(provider: str, written: dict[str, str]) -> str:
    """Текст подтверждения для per-user сохранения."""
    if provider == "none":
        return "AI выключен. Твои ключи сохранены, но не используются."
    if provider == "ollama":
        return "Сохранено: твой Ollama. Запросы идут на указанный тобой URL."
    if written:
        return (
            f"Сохранено. Твой провайдер: {provider}. "
            f"Ключи обновлены: {', '.join(written)}."
        )
    return (
        f"Твой провайдер: {provider}. Поля ключей пустые — "
        "старые ключи остались на месте."
    )


async def _save_user_choice(
    request: Request,
    user_id: int,
    provider: str,
    form: dict[str, str],
) -> HTMLResponse:
    """POST /settings/llm для НЕ-владельца: пишем только его user_settings.

    Ни kv, ни vault, ни мастер-пароль тут не участвуют: чужие ключи ему
    недоступны, а свои живут в его личном пространстве. ``worker`` не
    проходит валидацию, потому что его нет в ``_USER_ALLOWED_PROVIDERS``.
    """
    from app.llm.client import _USER_ALLOWED_PROVIDERS  # noqa: PLC0415

    if provider != "none" and provider not in _USER_ALLOWED_PROVIDERS:
        await log_action(
            "llm.switcher.save",
            target=provider,
            detail="bad provider (user)",
            success=False,
        )
        return _render(
            request,
            current_provider=await _user_current_provider(user_id),
            keys=await _user_key_status(user_id),
            error=(
                "Этот провайдер недоступен. Выбери своего — например свой "
                "Ollama или облачный ключ."
            ),
            is_owner_user=False,
            extras=await _extras_status(user_id),
            status_code=400,
        )

    parsed_extras, extras_error = _collect_extras(form, owner=False)
    if extras_error:
        return _render(
            request,
            current_provider=await _user_current_provider(user_id),
            keys=await _user_key_status(user_id),
            error=extras_error,
            is_owner_user=False,
            extras=await _extras_status(user_id),
            status_code=400,
        )

    written = await _persist_user_choice(user_id, provider, form, parsed_extras)
    await log_action(
        "llm.switcher.save",
        target=provider,
        detail="user_scope written=" + ",".join(written),
        success=True,
    )
    log.info("llm.switcher.saved_user", provider=provider, keys_written=list(written))
    return _render(
        request,
        current_provider=provider,
        keys=await _user_key_status(user_id),
        notice=_user_save_notice(provider, written),
        is_owner_user=False,
        extras=await _extras_status(user_id),
    )


# ---------------------------------------------------------------------------
# W-D: статус ПК-воркера (best-effort, ленивый импорт — зависит от W-A)
# ---------------------------------------------------------------------------


async def _worker_status_safe() -> dict[str, object] | None:
    """Текущий статус ПК-воркера (online/model/last_seen) или None.

    Ленивый импорт ``app.llm.worker_queue`` (модуль приходит из слайса W-A):
    если его ещё нет или вызов упал — тихо возвращаем None, чтобы страница
    провайдеров не падала из-за неприземлившейся зависимости.
    """
    try:
        from app.llm.worker_queue import worker_status  # noqa: PLC0415

        return await worker_status()
    except Exception:  # noqa: BLE001 — W-A может быть ещё не приземлён
        return None


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
    worker_token: str | None = None,
    worker_status: dict[str, object] | None = None,
    is_owner_user: bool = False,
    extras: dict[str, dict[str, str]] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Single render entry-point so every code path uses the same context."""
    # Безопасность: НЕ отдаём plaintext-значения ключей в HTML (утечка в history/
    # DevTools/кеш). Шаблон показывает только статус «настроен», не сам ключ.
    safe_keys = {
        slug: {k: v for k, v in entry.items() if k != "value"}
        for slug, entry in keys.items()
    }
    return templates.TemplateResponse(
        request,
        "llm_switcher.html",
        {
            "title": "AI провайдер",
            "active_nav": "settings",
            # Не-владельцу «worker» не показываем даже как вариант выбора.
            "providers": PROVIDERS if is_owner_user else _user_providers(),
            "current_provider": current_provider,
            "keys": safe_keys,
            "notice": notice,
            "error": error,
            "test_result": test_result,
            # W-D: разовый показ свежесгенерированного токена воркера +
            # текущий онлайн-статус ПК-воркера (для секции «worker»).
            "worker_token": worker_token,
            "worker_status": worker_status,
            "is_owner": is_owner_user,
            # Адрес эндпоинта + модель для OpenAI-совместимых сервисов.
            # Ключей здесь нет и быть не может — только адреса и имена моделей.
            "extras": extras or {},
            "url_editable": sorted(_URL_EDITABLE_SLUGS),
            "universal_slug": UNIVERSAL_SLUG,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/settings/llm", response_class=HTMLResponse, response_model=None)
async def llm_switcher_page(
    request: Request,
    session: SessionRecord = Depends(current_user_required),
) -> HTMLResponse:
    """Render the provider/key picker.

    No query parameters — the master password is never accepted via GET
    so it never lands in the access log or browser history.
    """
    uid = int(session["user_id"])
    owner = await is_owner(uid)
    if not owner:
        return _render(
            request,
            current_provider=await _user_current_provider(uid),
            keys=await _user_key_status(uid),
            is_owner_user=False,
            extras=await _extras_status(uid),
        )
    current = await _current_provider()
    keys = await _key_status_per_provider(master_password=None)
    log.info(
        "llm.switcher.render",
        current_provider=current,
        configured=[slug for slug, info in keys.items() if info["configured"] == "true"],
    )
    return _render(
        request,
        current_provider=current,
        keys=keys,
        worker_status=await _worker_status_safe(),
        is_owner_user=owner,
        extras=await _extras_status(None),
    )


@router.post("/settings/llm", response_class=HTMLResponse, response_model=None)
async def llm_switcher_save(
    request: Request,
    session: SessionRecord = Depends(current_user_required),
) -> HTMLResponse:
    """Persist the chosen provider and any newly-typed keys.

    T19 (2026-06-07): rewritten to read form data dynamically instead of
    hard-coding three providers. The previous version only accepted
    ``anthropic_api_key``, ``openai_api_key``, ``groq_api_key`` as Form
    parameters, so when T9/T10/T12 added 11 more providers (yandex,
    gigachat, deepseek, ollama, openrouter, mistral, together, xai,
    proxyapi, aitunnel, gemini) their key fields were silently dropped —
    user pasted a key, hit Save, kv_settings was never updated, and the
    page reloaded showing 'NOT SET' as if nothing happened.

    The fix reads ``await request.form()`` once and iterates over every
    slug in :data:`PROVIDERS`, so adding a new provider in the future
    just means appending a tuple — no route change needed.
    """
    form = await request.form()
    provider = str(form.get("provider", "")).strip().lower()
    master_password = str(form.get("master_password", ""))
    uid = int(session["user_id"])
    owner = await is_owner(uid)

    if not owner:
        plain_form = {k: str(v) for k, v in form.items() if isinstance(v, str)}
        return await _save_user_choice(request, uid, provider, plain_form)

    if provider not in _VALID_PROVIDERS:
        current = await _current_provider()
        keys = await _key_status_per_provider(master_password=None)
        log.warning("llm.switcher.bad_provider", provider=provider)
        await log_action(
            "llm.switcher.save",
            target=provider,
            detail="bad provider",
            success=False,
        )
        return _render(
            request,
            current_provider=current,
            keys=keys,
            error=f"Неизвестный провайдер «{provider}».",
            worker_status=await _worker_status_safe(),
            is_owner_user=owner,
            extras=await _extras_status(None),
            status_code=400,
        )

    plain_form = {k: str(v) for k, v in form.items() if isinstance(v, str)}
    parsed_extras, extras_error = _collect_extras(plain_form, owner=True)
    if extras_error:
        return _render(
            request,
            current_provider=await _current_provider(),
            keys=await _key_status_per_provider(master_password=None),
            error=extras_error,
            worker_status=await _worker_status_safe(),
            is_owner_user=owner,
            extras=await _extras_status(None),
            status_code=400,
        )

    await _persist_provider(provider)

    written: dict[str, str] = {}
    for slug, _label, _placeholder in PROVIDERS:
        raw = str(form.get(f"{slug}_api_key", "") or "").strip()
        if not raw:
            continue
        source = await _persist_key(slug, raw, master_password)
        written[slug] = source

    async with get_connection() as conn:
        for slug, (base_url, model) in parsed_extras.items():
            if base_url:
                await set_kv(conn, _base_url_kv(slug), base_url)
            if model:
                await set_kv(conn, _model_kv(slug), model)

    keys = await _key_status_per_provider(master_password=master_password or None)

    await log_action(
        "llm.switcher.save",
        target=provider,
        detail="written=" + ",".join(f"{p}:{src}" for p, src in written.items()),
        success=True,
    )
    log.info(
        "llm.switcher.saved",
        provider=provider,
        keys_written={slug: src for slug, src in written.items()},
    )

    if provider == "none":
        notice = "AI выключен. Ключи сохранены, но не используются."
    elif provider == "ollama":
        notice = "Сохранено: Ollama. Запросы идут на твой локальный сервер."
    elif provider == "worker":
        notice = (
            "Сохранено: Persona LLM Worker. Сгенерируй токен ниже и запусти "
            "persona_llm_worker на ПК — он сам будет забирать задачи (без туннеля)."
        )
    elif written:
        slug_written = ", ".join(written.keys())
        notice = (
            f"Сохранено. Активный провайдер: {provider}. "
            f"Ключи обновлены: {slug_written}."
        )
    else:
        notice = (
            f"Активный провайдер: {provider}. "
            "Поля ключей пустые — старые ключи остались на месте."
        )

    return _render(
        request,
        current_provider=provider,
        keys=keys,
        notice=notice,
        worker_status=await _worker_status_safe(),
        is_owner_user=owner,
        extras=await _extras_status(None),
    )


async def _test_user_config(request: Request, user_id: int) -> HTMLResponse:
    """Тот же «пинг», но собранный ИЗ КОНФИГА ПОЛЬЗОВАТЕЛЯ.

    ``make_client(user_id=...)`` для не-владельца читает только его
    ``user_settings``, поэтому кнопка «Проверить» проверяет именно его
    провайдера и его ключ, а не глобальный конфиг владельца.
    """
    import asyncio  # noqa: PLC0415

    current = await _user_current_provider(user_id)
    keys = await _user_key_status(user_id)
    extras = await _extras_status(user_id)

    def _out(result: str) -> HTMLResponse:
        return _render(
            request,
            current_provider=current,
            keys=keys,
            test_result=result,
            is_owner_user=False,
            extras=extras,
        )

    if current == "none":
        return _out("AI выключен (провайдер «никто»).")

    try:
        client = make_client(kind="llm_switcher_test", user_id=user_id)
    except LLMNotConfigured as exc:
        log.info("llm.switcher.test.user_not_configured", provider=current)
        return _out(f"Не настроено: {exc}")

    try:
        await asyncio.wait_for(
            client.complete(
                CompletionRequest(
                    system="Reply with the single word: pong.",
                    user="ping",
                    max_tokens=4,
                    temperature=0.0,
                )
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        log.info("llm.switcher.test.user_timeout", provider=current)
        return _out(
            "Таймаут (15с). Если это твой Ollama — первый запрос грузит модель "
            "в память, это 30-60 сек. Попробуй ещё раз."
        )
    except Exception as exc:  # noqa: BLE001 — текст ошибки, но НИКОГДА не ключ
        log.warning(
            "llm.switcher.test.user_fail",
            provider=current,
            error_type=type(exc).__name__,
        )
        return _out(f"Не получилось: {type(exc).__name__}")

    log.info("llm.switcher.test.user_ok", provider=current)
    return _out(f"OK — {current} ответил.")


@router.post("/settings/llm/test", response_class=HTMLResponse, response_model=None)
async def llm_switcher_test(
    request: Request,
    master_password: str = Form(""),
    session: SessionRecord = Depends(current_user_required),
) -> HTMLResponse:
    """Build a real client from the saved provider/key and send one prompt.

    The result string ("ok" or an error class + message — never the API
    key) is rendered back into the same page so the user gets immediate
    feedback without leaving ``/settings/llm``.
    """
    uid = int(session["user_id"])
    owner = await is_owner(uid)
    if not owner:
        return await _test_user_config(request, uid)

    current = await _current_provider()
    keys = await _key_status_per_provider(master_password=master_password or None)
    wstatus = await _worker_status_safe()
    owner_extras = await _extras_status(None)

    if current == "none":
        return _render(
            request,
            current_provider=current,
            keys=keys,
            test_result="AI features are disabled (provider=none).",
            worker_status=wstatus,
            is_owner_user=owner,
            extras=owner_extras,
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
            worker_status=wstatus,
            is_owner_user=owner,
            extras=owner_extras,
        )

    # T19 fix (2026-06-07) — bound the test call with asyncio timeout
    # so a cold-start Ollama (60+ sec to load model into VRAM) doesn't
    # freeze the entire uvicorn worker thread. 15s is plenty for a
    # warm provider; cold Ollama users see 'timeout' and a hint to
    # warm the model via /ask first.
    import asyncio  # noqa: PLC0415
    try:
        await asyncio.wait_for(
            client.complete(
                CompletionRequest(
                    system="Reply with the single word: pong.",
                    user="ping",
                    max_tokens=4,
                    temperature=0.0,
                )
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        log.info("llm.switcher.test.timeout", provider=current)
        return _render(
            request,
            current_provider=current,
            keys=keys,
            test_result=(
                "Timeout (15s). Если это Ollama — первый запрос грузит "
                "модель в VRAM, это 30-60 сек. Зайди в /ask, задай любой "
                "вопрос (можно ждать), потом нажми Тест ещё раз — будет "
                "быстро."
            ),
            worker_status=wstatus,
            is_owner_user=owner,
            extras=owner_extras,
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
            worker_status=wstatus,
            is_owner_user=owner,
            extras=owner_extras,
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
        worker_status=wstatus,
        is_owner_user=owner,
        extras=owner_extras,
    )
