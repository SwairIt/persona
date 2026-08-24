"""«Одолжить свою модель другу» + расширенный каталог провайдеров.

Инварианты, которые здесь закрепляются:

* каждый новый провайдер поднимается с ПРАВИЛЬНЫМ базовым URL, а
  ``openai_compatible`` без адреса или без модели не поднимается вовсе;
* анти-SSRF: метадата-адрес облака закрыт всем, петля сервера — участнику;
* участник без своей модели, но с живой выдачей, считает на модели друга —
  и НИ ОДИН ответ (HTML или JSON) не содержит чужого ключа;
* N+1-й запрос за сутки падает с отдельной ошибкой, а назавтра снова можно;
* отозванная / поставленная на паузу выдача = снова «не настроено»;
* владелец не затронут вообще;
* ``worker`` (домашний ПК владельца) достижим ТОЛЬКО через явную выдачу
  самого владельца.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import owner as owner_mod
from app.auth.users import create_user
from app.llm import grants as grants_mod
from app.llm.client import (
    LLMGrantQuotaExceeded,
    LLMNotConfigured,
    LLMProviderForbidden,
    PresetOpenAICompatibleClient,
    WorkerLLMClient,
    make_client,
    user_llm_configured,
)
from app.llm.providers import (
    PRESETS,
    PRESETS_BY_SLUG,
    InvalidBaseURL,
    normalise_chat_completions_url,
    validate_base_url,
)
from app.storage.repository import get_kv, set_kv, set_user_kv

OWNER_KEY = "sk-or-OWNER-SUPER-SECRET-KEY"
FRIEND_KEY = "sk-or-FRIEND-SUPER-SECRET-KEY"


def _reset_owner_cache() -> None:
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0


@pytest_asyncio.fixture
async def users(db: aiosqlite.Connection) -> dict[str, int]:
    """Владелец + двое обычных участников (даритель и получатель)."""
    owner = await create_user("owner@example.test", "owner-pass-123")
    friend = await create_user("friend@example.test", "friend-pass-123")
    member = await create_user("member@example.test", "member-pass-123")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    _reset_owner_cache()
    return {
        "owner": int(owner["id"]),
        "friend": int(friend["id"]),
        "member": int(member["id"]),
    }


async def _befriend(db: aiosqlite.Connection, a: int, b: int) -> None:
    """Подтверждённая дружба — две строки, как того требует миграция 229."""
    await db.execute(
        "INSERT OR IGNORE INTO friendship (user_id, friend_id) VALUES (?, ?)", (a, b)
    )
    await db.execute(
        "INSERT OR IGNORE INTO friendship (user_id, friend_id) VALUES (?, ?)", (b, a)
    )
    await db.commit()


async def _friend_with_openrouter(db: aiosqlite.Connection, users: dict[str, int]) -> None:
    """У друга настроен СВОЙ OpenRouter — именно его и будут одалживать."""
    await set_user_kv(db, users["friend"], "llm_provider", "openrouter")
    await set_user_kv(db, users["friend"], "byo_api_key_openrouter", FRIEND_KEY)
    await set_user_kv(db, users["friend"], "openrouter_model", "friend/model:free")


def _inner(client: object) -> object:
    return getattr(client, "_inner", client)


# ---------------------------------------------------------------------------
# A1. Каталог провайдеров
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", PRESETS, ids=[p.slug for p in PRESETS])
@pytest.mark.asyncio
async def test_every_preset_builds_with_its_base_url(
    db: aiosqlite.Connection, users: dict[str, int], preset
) -> None:
    """Каждый новый слаг собирается и стучится ИМЕННО в свой эндпоинт."""
    await set_user_kv(db, users["member"], "llm_provider", preset.slug)
    await set_user_kv(db, users["member"], f"byo_api_key_{preset.slug}", "test-key")

    client = make_client(kind="chat", user_id=users["member"])
    inner = _inner(client)
    assert isinstance(inner, PresetOpenAICompatibleClient)
    assert client.provider == preset.slug
    assert inner._BASE_URL == normalise_chat_completions_url(preset.base_url)
    assert inner._model == preset.default_model


@pytest.mark.asyncio
async def test_preset_base_url_is_user_overridable(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Дефолт пресета — только дефолт: свой адрес побеждает.

    Ради этого свойства адрес и вынесен в настройки: сервисы переезжают, и
    «зашитая константа» чинилась бы только релизом.
    """
    await set_user_kv(db, users["member"], "llm_provider", "cerebras")
    await set_user_kv(db, users["member"], "byo_api_key_cerebras", "csk-x")
    await set_user_kv(
        db, users["member"], "cerebras_base_url", "https://my-proxy.example.com/v1"
    )
    await set_user_kv(db, users["member"], "cerebras_model", "my-model")

    inner = _inner(make_client(kind="chat", user_id=users["member"]))
    assert inner._BASE_URL == "https://my-proxy.example.com/v1/chat/completions"
    assert inner._model == "my-model"


@pytest.mark.asyncio
async def test_openai_compatible_requires_url_and_model(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Универсальный провайдер бесполезен без адреса И имени модели."""
    uid = users["member"]
    await set_user_kv(db, uid, "llm_provider", "openai_compatible")

    with pytest.raises(LLMNotConfigured):  # нет ключа
        make_client(kind="chat", user_id=uid)

    await set_user_kv(db, uid, "byo_api_key_openai_compatible", "k")
    with pytest.raises(LLMNotConfigured) as exc_url:
        make_client(kind="chat", user_id=uid)
    assert "адрес" in str(exc_url.value).lower()

    await set_user_kv(db, uid, "openai_compatible_base_url", "https://api.example.com/v1")
    with pytest.raises(LLMNotConfigured) as exc_model:
        make_client(kind="chat", user_id=uid)
    assert "модел" in str(exc_model.value).lower()

    await set_user_kv(db, uid, "openai_compatible_model", "some-model")
    inner = _inner(make_client(kind="chat", user_id=uid))
    assert inner._BASE_URL == "https://api.example.com/v1/chat/completions"
    assert inner._model == "some-model"
    assert inner.provider == "openai_compatible"


# ---------------------------------------------------------------------------
# A2. Анти-SSRF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://169.254.169.254/v1",
        "http://metadata.google.internal/v1",
        "http://0.0.0.0:8080/v1",
    ],
)
def test_metadata_and_link_local_rejected_for_everyone(url: str) -> None:
    """Метадата облака закрыта ОБОИМ — и участнику, и владельцу."""
    for owner in (True, False):
        with pytest.raises(InvalidBaseURL):
            validate_base_url(url, owner=owner)


def test_server_loopback_rejected_for_member_allowed_for_owner() -> None:
    """127.0.0.1 для участника — это машина сервера, а не его собственная."""
    for url in ("http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:8080"):
        with pytest.raises(InvalidBaseURL):
            validate_base_url(url, owner=False)
        assert validate_base_url(url, owner=True)


def test_https_required_publicly_but_http_ok_in_lan() -> None:
    with pytest.raises(InvalidBaseURL):
        validate_base_url("http://api.example.com/v1", owner=False)
    assert validate_base_url("https://api.example.com/v1", owner=False)
    # Домашнее железо: сертификата нет, http — единственный честный вариант.
    for lan in ("http://192.168.1.10:11434", "http://10.0.0.5:8080", "http://nas.local:11434"):
        assert validate_base_url(lan, owner=False)


def test_credentials_in_url_rejected() -> None:
    with pytest.raises(InvalidBaseURL):
        validate_base_url("https://user:pass@api.example.com/v1", owner=True)


@pytest.mark.asyncio
async def test_member_openai_compatible_cannot_target_server_loopback(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Тот же запрет — не только в форме, но и на пути сборки клиента."""
    uid = users["member"]
    await set_user_kv(db, uid, "llm_provider", "openai_compatible")
    await set_user_kv(db, uid, "byo_api_key_openai_compatible", "k")
    await set_user_kv(db, uid, "openai_compatible_model", "m")
    await set_user_kv(db, uid, "openai_compatible_base_url", "http://127.0.0.1:11434/v1")
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=uid)

    await set_user_kv(
        db, uid, "openai_compatible_base_url", "http://169.254.169.254/v1"
    )
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=uid)


@pytest.mark.asyncio
async def test_member_ollama_cannot_target_server_loopback(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Дыра, которую закрыли заодно: раньше хватало «начинается на http».

    ``http://127.0.0.1:11434`` — это Ollama НА СЕРВЕРЕ, то есть железо
    владельца; ровно то, что per-user резолюция и должна была запретить.
    """
    uid = users["member"]
    await set_user_kv(db, uid, "llm_provider", "ollama")
    await set_user_kv(db, uid, "byo_api_key_ollama", "http://127.0.0.1:11434")
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=uid)

    # Свой LAN-адрес по-прежнему работает.
    await set_user_kv(db, uid, "byo_api_key_ollama", "http://192.168.1.10:11434")
    assert make_client(kind="chat", user_id=uid).provider == "ollama"


# ---------------------------------------------------------------------------
# B1. Выдача: клиент собирается из конфига ДАРИТЕЛЯ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_builds_client_from_grantor_config(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    await grants_mod.upsert_grant(users["friend"], users["member"], 5)

    client = make_client(kind="chat", user_id=users["member"])
    inner = _inner(client)
    assert client.provider == "openrouter"
    # Ключ и модель — ДАРИТЕЛЯ (extras читаются из его user_settings).
    assert inner._api_key == FRIEND_KEY
    assert inner._model == "friend/model:free"
    # Расход записан на ПОЛУЧАТЕЛЯ: потратил он.
    assert client._user_id == users["member"]
    # …и один запрос уже списан.
    assert await grants_mod.usage_today(1) == 1


@pytest.mark.asyncio
async def test_grantee_never_sees_the_key_in_any_response(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Ключ дарителя не должен появиться НИ в HTML страницы, НИ в JSON API."""
    from app.auth import current_user_required
    from app.web.routes import llm_models, llm_sharing, llm_switcher

    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    await grants_mod.upsert_grant(users["friend"], users["member"], 5, note="на недельку")

    app = FastAPI()
    app.include_router(llm_sharing.router)
    app.include_router(llm_switcher.router)
    app.include_router(llm_models.router)

    async def _fake_session() -> dict[str, object]:
        return {"user_id": users["member"], "id": 1}

    app.dependency_overrides[current_user_required] = _fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/settings/llm/sharing", "/settings/llm", "/api/llm/models"):
            response = await client.get(path)
            assert response.status_code == 200, (path, response.text[:300])
            assert FRIEND_KEY not in response.text, path
            assert "sk-or-FRIEND" not in response.text, path

        # Страница выдач всё же показывает ЧТО именно одолжено — имя провайдера.
        page = await client.get("/settings/llm/sharing")
        assert "friend@example.test" in page.text
        assert "openrouter" in page.text


# ---------------------------------------------------------------------------
# B2. Квота
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_blocks_request_n_plus_one_and_resets_next_day(
    db: aiosqlite.Connection, users: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N вызовов проходят, N+1-й падает; назавтра счётчик снова нулевой.

    Дату ПОДМЕНЯЕМ, а не спим: тест про сутки не имеет права идти сутки.
    """
    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    await grants_mod.upsert_grant(users["friend"], users["member"], 3)

    monkeypatch.setattr(grants_mod, "_today", lambda: "2026-08-24")
    for _ in range(3):
        assert make_client(kind="chat", user_id=users["member"]).provider == "openrouter"

    with pytest.raises(LLMGrantQuotaExceeded) as excinfo:
        make_client(kind="chat", user_id=users["member"])
    # Подкласс LLMNotConfigured — иначе ~40 мест с ``except LLMNotConfigured``
    # перестали бы деградировать мягко и посыпались бы 500-ками.
    assert isinstance(excinfo.value, LLMNotConfigured)
    assert "лимит" in str(excinfo.value).lower()

    monkeypatch.setattr(grants_mod, "_today", lambda: "2026-08-25")
    assert make_client(kind="chat", user_id=users["member"]).provider == "openrouter"


@pytest.mark.asyncio
async def test_quota_counter_is_exact_under_repeat(
    db: aiosqlite.Connection, users: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Счётчик — ровно число выданных клиентов, не больше и не меньше."""
    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    grant_id = await grants_mod.upsert_grant(users["friend"], users["member"], 10)
    monkeypatch.setattr(grants_mod, "_today", lambda: "2026-08-24")

    for expected in range(1, 6):
        make_client(kind="chat", user_id=users["member"])
        assert await grants_mod.usage_today(grant_id, "2026-08-24") == expected


@pytest.mark.asyncio
async def test_status_helper_does_not_burn_quota(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """«Настроен ли AI» спрашивают на каждой отрисовке — это не может стоить запроса."""
    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    grant_id = await grants_mod.upsert_grant(users["friend"], users["member"], 2)

    assert await user_llm_configured(users["member"]) is True
    assert await grants_mod.borrowed_status(users["member"]) is not None
    assert await grants_mod.usage_today(grant_id) == 0


# ---------------------------------------------------------------------------
# B3. Отзыв / пауза / дружба
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_grant_falls_back_to_not_configured(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    grant_id = await grants_mod.upsert_grant(users["friend"], users["member"], 5)
    assert make_client(kind="chat", user_id=users["member"]).provider == "openrouter"

    assert await grants_mod.revoke(users["friend"], grant_id) is True
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=users["member"])
    assert await user_llm_configured(users["member"]) is False


@pytest.mark.asyncio
async def test_disabled_grant_falls_back_to_not_configured(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    grant_id = await grants_mod.upsert_grant(users["friend"], users["member"], 5)

    assert await grants_mod.set_enabled(users["friend"], grant_id, False) is True
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=users["member"])

    assert await grants_mod.set_enabled(users["friend"], grant_id, True) is True
    assert make_client(kind="chat", user_id=users["member"]).provider == "openrouter"


@pytest.mark.asyncio
async def test_grant_between_non_friends_is_not_honoured(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Дружба есть в БД → она и требуется. Без неё выдача не действует."""
    await _friend_with_openrouter(db, users)
    await grants_mod.upsert_grant(users["friend"], users["member"], 5)
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=users["member"])

    await _befriend(db, users["friend"], users["member"])
    assert make_client(kind="chat", user_id=users["member"]).provider == "openrouter"


@pytest.mark.asyncio
async def test_explicit_ai_off_does_not_fall_through_to_a_grant(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """«Никто» — это ВЫБОР выключить, а не «нечем считать».

    Человек, который выключил AI, не должен внезапно обнаружить, что всё это
    время жёг чужую квоту.
    """
    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    grant_id = await grants_mod.upsert_grant(users["friend"], users["member"], 5)
    await set_user_kv(db, users["member"], "llm_provider", "none")

    with pytest.raises(LLMNotConfigured) as excinfo:
        make_client(kind="chat", user_id=users["member"])
    assert "выключен" in str(excinfo.value).lower()
    assert await grants_mod.usage_today(grant_id) == 0
    assert await user_llm_configured(users["member"]) is False


@pytest.mark.asyncio
async def test_friendship_check_is_defensive_when_table_missing(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Нет таблицы ``friendship`` (миграция 229 не приземлилась) → «друзья».

    Резолвер LLM не имеет права падать из-за чужого, ещё не приземлившегося
    модуля: выдача и так поимённая и явная.
    """

    class _RaisingConn:
        async def execute(self, *_args: object, **_kw: object) -> None:
            raise RuntimeError("no such table: friendship")

    assert await grants_mod._friends_ok(_RaisingConn(), 1, 2) is True


@pytest.mark.asyncio
async def test_only_the_grantor_can_change_or_revoke(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Чужой выдачей управлять нельзя — ни получателю, ни владельцу."""
    await _friend_with_openrouter(db, users)
    grant_id = await grants_mod.upsert_grant(users["friend"], users["member"], 5)

    assert await grants_mod.revoke(users["member"], grant_id) is False
    assert await grants_mod.set_limit(users["owner"], grant_id, 999) is False
    assert await grants_mod.set_enabled(users["member"], grant_id, False) is False
    assert await grants_mod.revoke(users["friend"], grant_id) is True


@pytest.mark.asyncio
async def test_own_config_wins_over_a_grant(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Своя модель приоритетнее одолженной — чужую квоту зря не жжём."""
    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    grant_id = await grants_mod.upsert_grant(users["friend"], users["member"], 5)

    await set_user_kv(db, users["member"], "llm_provider", "groq")
    await set_user_kv(db, users["member"], "byo_api_key_groq", "gsk-MY-OWN")

    client = make_client(kind="chat", user_id=users["member"])
    assert client.provider == "groq"
    assert _inner(client)._api_key == "gsk-MY-OWN"
    assert await grants_mod.usage_today(grant_id) == 0


# ---------------------------------------------------------------------------
# B4. worker достижим ТОЛЬКО через явную выдачу владельца
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_unreachable_without_grant(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    await set_kv(db, "llm_provider", "worker")
    await set_user_kv(db, users["member"], "llm_provider", "worker")
    with pytest.raises(LLMProviderForbidden):
        make_client(kind="chat", user_id=users["member"])


@pytest.mark.asyncio
async def test_worker_reachable_only_through_owner_grant(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """ПК владельца одалживается — но лишь тем, кому владелец выдал доступ явно."""
    await set_kv(db, "llm_provider", "worker")
    await set_kv(db, "ollama_model", "qwen2.5:7b")
    await _befriend(db, users["owner"], users["member"])
    await grants_mod.upsert_grant(users["owner"], users["member"], 4)

    client = make_client(kind="chat", user_id=users["member"])
    inner = _inner(client)
    assert isinstance(inner, WorkerLLMClient)
    assert client.provider == "worker"
    # Модель взята из ГЛОБАЛЬНОГО kv владельца (extras дарителя), а расход
    # записан на получателя.
    assert inner._model == "qwen2.5:7b"
    assert client._user_id == users["member"]

    # Другой участник, которому владелец ничего не выдавал, остаётся ни с чем.
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=users["friend"])


@pytest.mark.asyncio
async def test_member_cannot_relend_the_owners_worker(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Получатель не может «передарить» чужой ПК дальше по цепочке.

    Выдача участника резолвится ТОЛЬКО через его собственный
    ``user_settings``, где ``worker`` запрещён — то есть цепочка обрывается
    на первом же звене, а не «работает, просто мы надеемся, что никто не
    попробует».
    """
    await set_kv(db, "llm_provider", "worker")
    await _befriend(db, users["owner"], users["member"])
    await _befriend(db, users["member"], users["friend"])
    await grants_mod.upsert_grant(users["owner"], users["member"], 4)
    # member пробует одолжить дальше — friend'у.
    await grants_mod.upsert_grant(users["member"], users["friend"], 4)

    with pytest.raises(LLMNotConfigured) as excinfo:
        make_client(kind="chat", user_id=users["friend"])
    assert not isinstance(excinfo.value, LLMGrantQuotaExceeded)


# ---------------------------------------------------------------------------
# B5. Владелец не затронут
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_unaffected_by_grants(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """У владельца ``make_client()`` и ``make_client(user_id=owner)`` не изменились."""
    await set_kv(db, "llm_provider", "openrouter")
    await set_kv(db, "byo_api_key_openrouter", OWNER_KEY)
    # Кто-то выдал владельцу доступ — это не должно ничего поменять.
    await _friend_with_openrouter(db, users)
    await _befriend(db, users["friend"], users["owner"])
    grant_id = await grants_mod.upsert_grant(users["friend"], users["owner"], 5)

    global_client = make_client(kind="chat")
    owner_client = make_client(kind="chat", user_id=users["owner"])
    assert owner_client.provider == global_client.provider == "openrouter"
    assert _inner(owner_client)._api_key == _inner(global_client)._api_key == OWNER_KEY
    # Леджер владельца по-прежнему без user_id, чужая квота не тронута.
    assert owner_client._user_id is None
    assert await grants_mod.usage_today(grant_id) == 0


# ---------------------------------------------------------------------------
# B6. Страница выдач
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sharing_client(users: dict[str, int]):
    from app.auth import current_user_required
    from app.web.routes import llm_sharing

    app = FastAPI()
    app.include_router(llm_sharing.router)

    async def _fake_session() -> dict[str, object]:
        return {"user_id": users["friend"], "id": 1}

    app.dependency_overrides[current_user_required] = _fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_grant_form_creates_and_revokes(
    db: aiosqlite.Connection, users: dict[str, int], sharing_client: AsyncClient
) -> None:
    await _friend_with_openrouter(db, users)

    response = await sharing_client.post(
        "/settings/llm/sharing/grant",
        data={"email": "member@example.test", "daily_limit": "7", "note": "на неделю"},
    )
    assert response.status_code == 200, response.text
    issued = await grants_mod.list_issued_by(users["friend"])
    assert len(issued) == 1
    assert issued[0]["daily_limit"] == 7
    assert issued[0]["grantee_email"] == "member@example.test"

    revoked = await sharing_client.post(f"/settings/llm/sharing/{issued[0]['id']}/revoke")
    assert revoked.status_code == 200
    assert await grants_mod.list_issued_by(users["friend"]) == []


@pytest.mark.asyncio
async def test_grant_form_rejects_bad_limit_and_unknown_email(
    db: aiosqlite.Connection, users: dict[str, int], sharing_client: AsyncClient
) -> None:
    zero = await sharing_client.post(
        "/settings/llm/sharing/grant",
        data={"email": "member@example.test", "daily_limit": "0"},
    )
    assert zero.status_code == 400
    assert await grants_mod.list_issued_by(users["friend"]) == []

    unknown = await sharing_client.post(
        "/settings/llm/sharing/grant",
        data={"email": "nobody@example.test", "daily_limit": "5"},
    )
    assert unknown.status_code == 400
    assert await grants_mod.list_issued_by(users["friend"]) == []

    myself = await sharing_client.post(
        "/settings/llm/sharing/grant",
        data={"email": "friend@example.test", "daily_limit": "5"},
    )
    assert myself.status_code == 400


@pytest.mark.asyncio
async def test_repeat_grant_updates_the_same_row(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """UNIQUE(grantor, grantee): повторная выдача правит строку, а не плодит вторую."""
    first = await grants_mod.upsert_grant(users["friend"], users["member"], 5)
    second = await grants_mod.upsert_grant(users["friend"], users["member"], 42, "новее")
    assert first == second
    issued = await grants_mod.list_issued_by(users["friend"])
    assert len(issued) == 1
    assert issued[0]["daily_limit"] == 42
    assert issued[0]["note"] == "новее"


# ---------------------------------------------------------------------------
# B7. Бейдж чата честно называет одолженную модель
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_badge_says_the_model_is_borrowed(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    from app.web.routes.chat_sessions import _provider_badge

    await _befriend(db, users["friend"], users["member"])
    await _friend_with_openrouter(db, users)
    await grants_mod.upsert_grant(users["friend"], users["member"], 3)

    badge = await _provider_badge(users["member"])
    assert "друга" in str(badge["provider"])
    assert badge["borrowed"]["remaining"] == 3
    assert badge["borrowed"]["exhausted"] is False
    # Ключ дарителя в бейдж не попадает ни под каким видом.
    assert FRIEND_KEY not in str(badge)


# ---------------------------------------------------------------------------
# Каталог: слаги согласованы между данными, литералом Provider и формой
# ---------------------------------------------------------------------------


def test_preset_slugs_are_wired_everywhere() -> None:
    from app.llm.client import _ALL_PROVIDERS, _USER_ALLOWED_PROVIDERS
    from app.web.routes.llm_switcher import PROVIDERS as FORM_PROVIDERS

    form_slugs = {slug for slug, _label, _placeholder in FORM_PROVIDERS}
    for slug in PRESETS_BY_SLUG:
        assert slug in _ALL_PROVIDERS, slug
        assert slug in _USER_ALLOWED_PROVIDERS, slug
        assert slug in form_slugs, slug
    assert "openai_compatible" in form_slugs
    # worker остаётся вычеркнутым у участника — это ПК владельца.
    assert "worker" not in _USER_ALLOWED_PROVIDERS
