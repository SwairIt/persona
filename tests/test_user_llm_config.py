"""Per-user LLM config: чужой аккаунт НИКОГДА не считает на конфиге владельца.

Контракт, который здесь закрепляется:

* ``make_client()`` без ``user_id`` — ровно прежнее поведение (фоновые задачи).
* ``make_client(user_id=<владелец>)`` — тоже прежнее (владелец = глобальный kv).
* ``make_client(user_id=<обычный юзер>)`` — только его ``user_settings``:
  никакого фолбэка на kv/env/vault, провайдер ``worker`` (домашний ПК
  владельца) запрещён, Ollama без СВОЕГО URL не поднимается (иначе тихо
  уехали бы на localhost сервера).
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import owner as owner_mod
from app.auth.users import create_user
from app.llm.client import (
    LLMNotConfigured,
    LLMProviderForbidden,
    OllamaClient,
    OpenRouterClient,
    make_client,
    user_llm_configured,
)
from app.storage.repository import get_kv, set_kv, set_user_kv


def _reset_owner_cache() -> None:
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0


@pytest_asyncio.fixture
async def users(db: aiosqlite.Connection) -> dict[str, int]:
    """Владелец (минимальный id) + обычный зарегистрированный пользователь."""
    owner = await create_user("owner@example.test", "Zq7-frost-lantern-91")
    member = await create_user("member@example.test", "Kp4-velvet-harbour-38")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    _reset_owner_cache()
    return {"owner": int(owner["id"]), "member": int(member["id"])}


def _inner(client: object) -> object:
    return getattr(client, "_inner", client)


# ---------------------------------------------------------------------------
# 1-4: строгая per-user резолюция
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_without_settings_raises(users: dict[str, int]) -> None:
    """Пустой user_settings → «не настроено», а НЕ тихий переход на владельца."""
    with pytest.raises(LLMNotConfigured) as excinfo:
        make_client(kind="chat", user_id=users["member"])
    assert "/settings/llm" in str(excinfo.value)


@pytest.mark.asyncio
async def test_member_cannot_use_worker(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """``worker`` — домашний ПК владельца: запрещён даже при явном выборе."""
    await set_user_kv(db, users["member"], "llm_provider", "worker")
    with pytest.raises(LLMProviderForbidden) as excinfo:
        make_client(kind="chat", user_id=users["member"])
    # Подкласс LLMNotConfigured — иначе ~40 мест с ``except LLMNotConfigured``
    # перестали бы деградировать мягко и посыпались бы 500-ками.
    assert isinstance(excinfo.value, LLMNotConfigured)


@pytest.mark.asyncio
async def test_member_ollama_needs_own_url(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Ollama без СВОЕГО URL не поднимается — даже если глобальный kv настроен."""
    # Ollama владельца настроена глобально — она НЕ должна протечь.
    await set_kv(db, "byo_api_key_ollama", "http://owner-pc.local:11434")
    await set_user_kv(db, users["member"], "llm_provider", "ollama")

    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=users["member"])

    # Со своим URL — работает, и endpoint именно его.
    await set_user_kv(
        db, users["member"], "byo_api_key_ollama", "http://member-pc.local:11434"
    )
    client = make_client(kind="chat", user_id=users["member"])
    inner = _inner(client)
    assert isinstance(inner, OllamaClient)
    assert inner._endpoint == "http://member-pc.local:11434"
    assert "owner-pc" not in inner._endpoint


@pytest.mark.asyncio
async def test_member_ollama_garbage_url_does_not_fall_back_to_localhost(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Мусор вместо URL → ошибка, а не молчаливый localhost сервера.

    ``OllamaClient`` сам откатывается на ``http://localhost:11434`` для
    не-URL значений — для чужого аккаунта это была бы Ollama на машине
    сервера, поэтому per-user ветка обязана отбить такое РАНЬШЕ.
    """
    await set_user_kv(db, users["member"], "llm_provider", "ollama")
    await set_user_kv(db, users["member"], "byo_api_key_ollama", "sk-not-a-url")
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=users["member"])


@pytest.mark.asyncio
async def test_member_uses_own_openrouter_key(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Ключ берётся из user_settings; глобальный kv не читается и не пишется."""
    await set_kv(db, "llm_provider", "worker")
    await set_kv(db, "byo_api_key_openrouter", "sk-or-OWNER-KEY")
    await set_user_kv(db, users["member"], "llm_provider", "openrouter")
    await set_user_kv(db, users["member"], "byo_api_key_openrouter", "sk-or-MEMBER-KEY")
    await set_user_kv(db, users["member"], "openrouter_model", "member/model:free")

    client = make_client(kind="chat", user_id=users["member"])
    inner = _inner(client)
    assert isinstance(inner, OpenRouterClient)
    assert client.provider == "openrouter"
    assert inner._api_key == "sk-or-MEMBER-KEY"
    assert inner._model == "member/model:free"

    # Глобальные строки владельца не тронуты.
    assert await get_kv(db, "llm_provider") == "worker"
    assert await get_kv(db, "byo_api_key_openrouter") == "sk-or-OWNER-KEY"


@pytest.mark.asyncio
async def test_member_missing_key_raises(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Провайдер выбран, ключа нет → ошибка (а не ключ владельца)."""
    await set_kv(db, "byo_api_key_groq", "gsk-OWNER")
    await set_user_kv(db, users["member"], "llm_provider", "groq")
    with pytest.raises(LLMNotConfigured):
        make_client(kind="chat", user_id=users["member"])


# ---------------------------------------------------------------------------
# 5: владелец — байт-в-байт как раньше
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_with_user_id_matches_global(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """``make_client(user_id=owner)`` == ``make_client()`` по провайдеру и ключу."""
    await set_kv(db, "llm_provider", "openrouter")
    await set_kv(db, "byo_api_key_openrouter", "sk-or-OWNER-KEY")

    global_client = make_client(kind="chat")
    owner_client = make_client(kind="chat", user_id=users["owner"])

    assert owner_client.provider == global_client.provider == "openrouter"
    assert _inner(owner_client)._api_key == _inner(global_client)._api_key == "sk-or-OWNER-KEY"


@pytest.mark.asyncio
async def test_owner_keeps_worker_provider(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """У владельца worker остаётся рабочим — на нём крутится прод-чат."""
    await set_kv(db, "llm_provider", "worker")
    client = make_client(kind="chat", user_id=users["owner"])
    assert client.provider == "worker"


@pytest.mark.asyncio
async def test_full_access_user_is_treated_as_owner(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """kv full_access_user_ids даёт owner-эквивалент, включая worker."""
    await set_kv(db, "llm_provider", "worker")
    await set_kv(db, "full_access_user_ids", str(users["member"]))
    _reset_owner_cache()
    client = make_client(kind="chat", user_id=users["member"])
    assert client.provider == "worker"


# ---------------------------------------------------------------------------
# 6: model-switch не пишет глобальный kv
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def chat_client(users: dict[str, int]):
    """ASGI-клиент с роутом чата и подменённой сессией на member-а."""
    from app.auth import current_user_required
    from app.web.routes import chat_sessions

    app = FastAPI()
    app.include_router(chat_sessions.router)

    async def _fake_session() -> dict[str, object]:
        return {"user_id": users["member"], "id": 1}

    app.dependency_overrides[current_user_required] = _fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_model_switch_writes_user_scope_only(
    db: aiosqlite.Connection,
    users: dict[str, int],
    chat_client: AsyncClient,
) -> None:
    """Пикер модели у не-владельца пишет user_settings, не глобальный kv."""
    from app.chat.sessions import create_session
    from app.storage.repository import get_user_kv

    await set_kv(db, "llm_provider", "worker")
    session = await create_session(users["member"], title="t")

    response = await chat_client.post(
        f"/api/chat/sessions/{session['id']}/model",
        json={"provider": "openrouter", "model": "some/model"},
    )
    assert response.status_code == 200, response.text

    assert await get_user_kv(db, users["member"], "llm_provider") == "openrouter"
    assert await get_user_kv(db, users["member"], "openrouter_model") == "some/model"
    # Глобальный конфиг владельца НЕ тронут — раньше именно он и перезаписывался.
    assert await get_kv(db, "llm_provider") == "worker"


@pytest.mark.asyncio
async def test_model_switch_rejects_worker(
    db: aiosqlite.Connection,
    users: dict[str, int],
    chat_client: AsyncClient,
) -> None:
    """Выбрать ПК владельца через API нельзя."""
    from app.chat.sessions import create_session
    from app.storage.repository import get_user_kv

    session = await create_session(users["member"], title="t")
    response = await chat_client.post(
        f"/api/chat/sessions/{session['id']}/model",
        json={"provider": "worker", "model": "qwen2.5:3b"},
    )
    assert response.status_code == 403
    assert await get_user_kv(db, users["member"], "llm_provider") is None


# ---------------------------------------------------------------------------
# 7: /api/llm/models не светит конфиг владельца
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_models_endpoint_hides_owner_config(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    from app.auth import current_user_required
    from app.web.routes import llm_models

    await set_kv(db, "llm_provider", "worker")
    await set_kv(db, "byo_api_key_ollama", "http://owner-pc.local:11434")
    await set_kv(db, "worker_models", "owner-secret-model:7b")

    app = FastAPI()
    app.include_router(llm_models.router)

    async def _fake_session() -> dict[str, object]:
        return {"user_id": users["member"], "id": 1}

    app.dependency_overrides[current_user_required] = _fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/llm/models")

    assert response.status_code == 200
    payload = response.json()
    slugs = {p["slug"] for p in payload["providers"]}
    assert "worker" not in slugs

    body = response.text
    assert "owner-pc.local" not in body
    assert "owner-secret-model" not in body

    # Провайдер показывается СВОЙ (у member-а он не настроен), не worker.
    assert payload["current"]["provider"] != "worker"

    # Ollama без своего URL — не configured и с подсказкой, а не с чужими моделями.
    ollama = next(p for p in payload["providers"] if p["slug"] == "ollama")
    assert ollama["configured"] is False
    assert ollama["models"] == []
    assert "hint" in ollama


# ---------------------------------------------------------------------------
# 8: user_llm_configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_llm_configured_transitions(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    assert await user_llm_configured(users["member"]) is False

    await set_user_kv(db, users["member"], "llm_provider", "openrouter")
    # Провайдер без ключа — всё ещё «не настроено».
    assert await user_llm_configured(users["member"]) is False

    await set_user_kv(db, users["member"], "byo_api_key_openrouter", "sk-or-MEMBER")
    assert await user_llm_configured(users["member"]) is True


@pytest.mark.asyncio
async def test_user_llm_configured_never_raises(users: dict[str, int]) -> None:
    """Даже на несуществующем id функция обязана вернуть bool, а не упасть."""
    assert await user_llm_configured(10_000_000) is False


# ---------------------------------------------------------------------------
# Леджер расхода: чей вызов — того и строка
# ---------------------------------------------------------------------------


class _StubInner:
    provider = "openrouter"
    last_input_tokens = 11
    last_output_tokens = 22

    async def complete(self, _request: object) -> str:
        return "ok"


@pytest.mark.asyncio
async def test_borrowed_model_records_the_spender_not_the_lender(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Одолженная модель: строка расхода на ПОЛУЧАТЕЛЯ, а не на дарителя.

    Иначе счёт друга выглядел бы так, будто это он сидел в чате, — и человек
    не смог бы понять, куда ушли его токены.
    """
    from app.llm import grants as grants_mod

    lender = await create_user("lender@example.test", "lender-pass-123")
    lender_id = int(lender["id"])
    await db.execute(
        "INSERT OR IGNORE INTO friendship (user_id, friend_id) VALUES (?, ?)",
        (lender_id, users["member"]),
    )
    await db.commit()
    await set_user_kv(db, lender_id, "llm_provider", "openrouter")
    await set_user_kv(db, lender_id, "byo_api_key_openrouter", "sk-or-LENDER-KEY")
    await grants_mod.upsert_grant(lender_id, users["member"], 5)

    client = make_client(kind="chat", user_id=users["member"])
    assert client.provider == "openrouter"
    assert client._user_id == users["member"]
    assert _inner(client)._api_key == "sk-or-LENDER-KEY"


@pytest.mark.asyncio
async def test_member_without_settings_and_without_grant_still_raises(
    users: dict[str, int]
) -> None:
    """Без своей модели И без выдачи — по-прежнему честное «не настроено»."""
    with pytest.raises(LLMNotConfigured) as excinfo:
        make_client(kind="chat", user_id=users["member"])
    assert "/settings/llm" in str(excinfo.value)


@pytest.mark.asyncio
async def test_universal_provider_is_allowed_for_members(
    db: aiosqlite.Connection, users: dict[str, int]
) -> None:
    """Универсальный OpenAI-совместимый доступен участнику наравне с прочими."""
    from app.llm.client import _USER_ALLOWED_PROVIDERS

    assert "openai_compatible" in _USER_ALLOWED_PROVIDERS
    uid = users["member"]
    await set_user_kv(db, uid, "llm_provider", "openai_compatible")
    await set_user_kv(db, uid, "byo_api_key_openai_compatible", "sk-MINE")
    await set_user_kv(db, uid, "openai_compatible_base_url", "https://api.mine.test/v1")
    await set_user_kv(db, uid, "openai_compatible_model", "mine-1")

    inner = _inner(make_client(kind="chat", user_id=uid))
    assert inner._api_key == "sk-MINE"
    assert inner._BASE_URL == "https://api.mine.test/v1/chat/completions"


@pytest.mark.asyncio
async def test_usage_row_records_user_id(db: aiosqlite.Connection) -> None:
    """Расход не-владельца пишется с его user_id, глобальный — с NULL."""
    from app.llm.client import _UsageRecordingClient

    await _UsageRecordingClient(_StubInner(), kind="chat", user_id=42).complete(None)
    await _UsageRecordingClient(_StubInner(), kind="chat").complete(None)

    cursor = await db.execute("SELECT kind, user_id FROM llm_usage ORDER BY id")
    rows = [(r["kind"], r["user_id"]) for r in await cursor.fetchall()]
    assert rows == [("chat", 42), ("chat", None)]
