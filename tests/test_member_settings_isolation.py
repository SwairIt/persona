"""Личные настройки участника НЕ трогают (и не показывают) настройки владельца.

Контракт, который здесь закрепляется:

* ``/settings/system-prompt`` — участник видит и правит СВОЙ промпт
  (``user_settings.chat_system_prompt``); глобальная kv-строка владельца не
  меняется и в его разметку не попадает.
* ``/settings/theme`` — тема участника живёт в ``user_settings``; глобальный
  ``kv_settings.theme`` (тема ВСЕГО инстанса) остаётся владельцу.
* ``/settings/advanced`` — ``advanced_mode``/``feat_*`` участника пер-юзерные;
  ``get_advanced_flags(uid)`` отдаёт ИХ, глобальные флаги не тронуты.
* ``POST /settings/memory/engine`` — движок памяти инстанс-глобальный, поэтому
  участнику 403.
* ``POST /api/settings/ui-language`` — язык участника только его; интерфейс
  владельца остаётся на ``ru``.
* копилот ищет настройки по member-каталогу → ни одной owner-only ссылки.
* установка навыка ходит только на GitHub по https (SSRF-аллоулист).

kv ``owner_exclusive_mode`` тут ВЫКЛ — иначе гейт паркует всех не-владельцев
на /pending (отдельный kill-switch, см. test_owner_exclusive_lockdown.py).
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.repository import get_kv, get_user_kv, set_kv, set_user_kv
from app.web import templates_engine
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware

OWNER_PROMPT = "СЕКРЕТНЫЙ характер владельца: зови меня Ярослав, помни про Persona."


def _contains(body: str, text: str) -> bool:
    """Есть ли ``text`` в разметке — сырым ИЛИ json-экранированным.

    Промпт уезжает в Alpine через ``|tojson`` (``ensure_ascii=True``), поэтому
    кириллица в разметке выглядит как ``\\uXXXX``: наивное ``in`` дало бы
    ложно-зелёный негативный тест на утечку промпта владельца.
    """
    return text in body or json.dumps(text, ensure_ascii=True)[1:-1] in body


def _reset_caches() -> None:
    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    owner._fa_cache["value"] = None
    owner._fa_cache["checked_at"] = 0.0
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._role_gate_cache["value"] = False
    auth_gate._role_gate_cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0
    # Процесс-глобальные TTL-кэши синхронных читателей: между тестами БД
    # другая, поэтому без сброса тема/язык протекли бы из прошлого теста.
    templates_engine._kv_value_cache.clear()
    templates_engine._user_kv_value_cache.clear()
    # ContextVar с темой ТЕКУЩЕГО запроса живёт отдельно от dict-кэшей выше и
    # переживает границу теста: если предыдущий модуль оставил в нём значение,
    # ``get_theme()`` вернёт его, не заглянув в БД, и владелец увидит чужую
    # тему (падало именно так — на дефолтной ``persona`` вместо ``cosmos``).
    templates_engine.invalidate_theme_cache()
    i18n.invalidate_language_cache()


def _app():
    from fastapi import FastAPI

    from app.web.routes import (
        advanced_settings,
        dynamic_prompt_settings,
        memory_settings,
        system_prompt_settings,
        theme,
    )

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(system_prompt_settings.router)
    app.include_router(dynamic_prompt_settings.router)
    app.include_router(theme.router)
    app.include_router(advanced_settings.router)
    app.include_router(memory_settings.router)
    return app


@pytest_asyncio.fixture
async def env(db: aiosqlite.Connection):
    owner_user = await create_user("owner@iso.test", "Zq7-frost-lantern-91")
    member_user = await create_user("member@iso.test", "Kp4-velvet-harbour-38")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    # Настройки ВЛАДЕЛЬЦА, которые не должны ни утечь, ни перезаписаться.
    await set_kv(db, "chat_system_prompt", OWNER_PROMPT)
    await set_kv(db, "theme", "cosmos")
    await set_kv(db, "ui_language", "ru")
    await set_kv(db, "advanced_mode", "1")
    await set_kv(db, "feat_tools", "1")
    _reset_caches()

    transport = ASGITransport(app=_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db, owner_user, member_user
    finally:
        _reset_caches()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


# ── A. Системный промпт ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_prompt_page_never_shows_owner_text(env) -> None:
    client, _db, _owner_user, member_user = env
    await _as(client, member_user["id"])

    r = await client.get("/settings/system-prompt", follow_redirects=False)
    assert r.status_code == 200
    assert not _contains(r.text, OWNER_PROMPT)
    # …и ссылку в owner-only историю участнику не рисуем.
    assert "/settings/system-prompt/history" not in r.text


@pytest.mark.asyncio
async def test_member_cannot_open_prompt_history(env) -> None:
    """``/settings/system-prompt/history`` ЛОВИТСЯ member-префиксом.

    Гейт матчит вход в зону (``p + "/"``), поэтому история адаптивного
    характера участнику доступна по маршруту — её закрывает собственная
    зависимость роута (``is_primary_owner`` → 403). Сторожим именно это:
    если кто-то снимет проверку в dynamic_prompt_settings, дыра откроется
    молча, без правки гейта.
    """
    client, _db, owner_user, member_user = env
    assert auth_gate._is_member_path("/settings/system-prompt/history") is True

    await _as(client, member_user["id"])
    assert (
        await client.get("/settings/system-prompt/history", follow_redirects=False)
    ).status_code == 403

    await _as(client, owner_user["id"])
    assert (
        await client.get("/settings/system-prompt/history", follow_redirects=False)
    ).status_code == 200


@pytest.mark.asyncio
async def test_member_prompt_save_is_per_user(env) -> None:
    client, db, _owner_user, member_user = env
    await _as(client, member_user["id"])

    r = await client.post(
        "/settings/system-prompt",
        data={"prompt_text": "Мой собственный промпт участника"},
        follow_redirects=False,
    )
    assert r.status_code == 200

    assert (
        await get_user_kv(db, member_user["id"], "chat_system_prompt")
    ) == "Мой собственный промпт участника"
    # Глобальная строка владельца НЕ тронута.
    assert (await get_kv(db, "chat_system_prompt")) == OWNER_PROMPT

    body = (await client.get("/settings/system-prompt")).text
    assert _contains(body, "Мой собственный промпт участника")
    assert not _contains(body, OWNER_PROMPT)


@pytest.mark.asyncio
async def test_owner_prompt_save_still_global(env) -> None:
    client, db, owner_user, member_user = env
    await _as(client, owner_user["id"])

    await client.post(
        "/settings/system-prompt",
        data={"prompt_text": "Новый глобальный промпт"},
        follow_redirects=False,
    )
    assert (await get_kv(db, "chat_system_prompt")) == "Новый глобальный промпт"
    assert (await get_user_kv(db, owner_user["id"], "chat_system_prompt")) is None
    assert (await get_user_kv(db, member_user["id"], "chat_system_prompt")) is None


@pytest.mark.asyncio
async def test_member_prompt_resolves_to_default_not_owner(env) -> None:
    """Нет своей строки → встроенный дефолт, а НЕ текст владельца."""
    from app.chat.prompts import DEFAULT_SYSTEM_PROMPT, get_active_system_prompt

    _client, _db, owner_user, member_user = env
    assert await get_active_system_prompt(member_user["id"]) == DEFAULT_SYSTEM_PROMPT
    assert await get_active_system_prompt(owner_user["id"]) == OWNER_PROMPT
    # Фоновые вызовы без user_id — прежний глобальный путь.
    assert await get_active_system_prompt() == OWNER_PROMPT


# ── B. Тема + язык ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_theme_is_per_user(env) -> None:
    client, db, owner_user, member_user = env
    await _as(client, member_user["id"])

    r = await client.post(
        "/settings/theme", data={"theme": "light"}, follow_redirects=False
    )
    assert r.status_code == 303

    assert (await get_user_kv(db, member_user["id"], "theme")) == "light"
    assert (await get_kv(db, "theme")) == "cosmos"  # тема владельца цела

    # Разметка участника рендерится ЕГО темой…
    member_body = (await client.get("/settings/theme")).text
    assert "theme-cosmos" not in member_body

    # …а владелец по-прежнему видит свою.
    await _as(client, owner_user["id"])
    owner_body = (await client.get("/settings/theme")).text
    assert "theme-cosmos" in owner_body


@pytest.mark.asyncio
async def test_member_ui_language_is_per_user(env) -> None:
    client, db, owner_user, member_user = env
    await _as(client, member_user["id"])

    r = await client.post(
        "/api/settings/ui-language", data={"language": "en"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert (await get_user_kv(db, member_user["id"], "ui_language")) == "en"
    assert (await get_kv(db, "ui_language")) == "ru"

    member_body = (await client.get("/settings/theme")).text
    assert '<html lang="en"' in member_body

    await _as(client, owner_user["id"])
    owner_body = (await client.get("/settings/theme")).text
    assert '<html lang="ru"' in owner_body


@pytest.mark.asyncio
async def test_owner_ui_language_still_global(env) -> None:
    client, db, owner_user, _member_user = env
    await _as(client, owner_user["id"])

    await client.post(
        "/api/settings/ui-language", data={"language": "de"}, follow_redirects=False
    )
    assert (await get_kv(db, "ui_language")) == "de"
    assert (await get_user_kv(db, owner_user["id"], "ui_language")) is None


@pytest.mark.asyncio
async def test_ui_language_path_is_member_reachable() -> None:
    assert auth_gate._is_member_path("/api/settings/ui-language") is True


# ── C. Расширенные функции ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_advanced_toggle_is_per_user(env) -> None:
    from app.web.routes.chat_sessions import get_advanced_flags

    client, db, owner_user, member_user = env
    await _as(client, member_user["id"])

    # Мастер ВКЛ, но инструменты выключаем.
    r = await client.post(
        "/settings/advanced",
        data={"master": "1", "effort": "1", "modes": "1", "choices": "1",
              "auto_prompt": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    member_flags = await get_advanced_flags(member_user["id"])
    assert member_flags["master"] is True
    assert member_flags["tools"] is False

    # Глобальные флаги владельца не тронуты.
    assert (await get_kv(db, "feat_tools")) == "1"
    owner_flags = await get_advanced_flags(owner_user["id"])
    assert owner_flags["tools"] is True
    # Фоновый вызов без user_id — прежний глобальный путь.
    assert (await get_advanced_flags())["tools"] is True


@pytest.mark.asyncio
async def test_member_advanced_page_hides_recall_control(env) -> None:
    client, _db, owner_user, member_user = env

    await _as(client, member_user["id"])
    member_body = (await client.get("/settings/advanced")).text
    assert 'name="recall_mode"' not in member_body

    await _as(client, owner_user["id"])
    owner_body = (await client.get("/settings/advanced")).text
    assert 'name="recall_mode"' in owner_body


@pytest.mark.asyncio
async def test_member_cannot_write_global_recall_mode(env) -> None:
    """Подделанный POST с recall_mode не должен трогать глобальную строку."""
    client, db, _owner_user, member_user = env
    await set_kv(db, "recall_mode", "hybrid")
    await _as(client, member_user["id"])

    await client.post(
        "/settings/advanced",
        data={"master": "1", "recall_mode": "vector"},
        follow_redirects=False,
    )
    assert (await get_kv(db, "recall_mode")) == "hybrid"


# ── D. Движок памяти — owner-only ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_cannot_save_memory_engine(env) -> None:
    client, db, _owner_user, member_user = env
    await set_kv(db, "dream_enabled", "0")
    await _as(client, member_user["id"])

    r = await client.post(
        "/settings/memory/engine",
        data={"dream_enabled": "1", "dream_hour_local": "5"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert (await get_kv(db, "dream_enabled")) == "0"


@pytest.mark.asyncio
async def test_owner_still_saves_memory_engine(env) -> None:
    client, db, owner_user, _member_user = env
    await _as(client, owner_user["id"])

    r = await client.post(
        "/settings/memory/engine",
        data={"dream_enabled": "1", "dream_hour_local": "5"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (await get_kv(db, "dream_enabled")) == "1"
    assert (await get_kv(db, "dream_hour_local")) == "5"


@pytest.mark.asyncio
async def test_member_memory_page_hides_engine_and_train_result(env) -> None:
    client, db, _owner_user, member_user = env
    await set_kv(db, "train_last_result", "Готово: кандидатов 12, запомнено 3")
    await _as(client, member_user["id"])

    body = (await client.get("/settings/memory")).text
    assert "/settings/memory/engine" not in body
    assert not _contains(body, "Готово: кандидатов 12")
    # Личные факты участника при этом на месте — форма добавления работает.
    assert "/settings/memory/add" in body


# ── E. Копилот: поиск настроек по member-каталогу ──────────────────────────


def test_copilot_settings_block_hides_owner_only_pages() -> None:
    from app.llm.copilot_stream import _find_settings_block

    owner_block = _find_settings_block("настройки", member=False)
    member_block = _find_settings_block("настройки", member=True)

    owner_only = ("/settings/capture", "/settings/ocr", "/root", "/admin")
    for href in owner_only:
        assert href not in member_block
    # У владельца каталог шире — иначе тест ничего не сторожил бы.
    assert len(owner_block) >= len(member_block)


@pytest.mark.asyncio
async def test_copilot_member_flag_resolves(env) -> None:
    from app.llm.copilot_stream import _is_member

    _client, _db, owner_user, member_user = env
    assert await _is_member(member_user["id"]) is True
    assert await _is_member(owner_user["id"]) is False
    assert await _is_member(None) is False


# ── F. Установка навыка: SSRF-аллоулист ────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/github.com/user/repo",
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.example.com/user/repo",
        "https://evil.example.com/github.com/user/repo",
        "http://github.com/user/repo",  # https-only
        "https://github.com.evil.example/user/repo",
        "file:///etc/passwd",
        "https://raw.githubusercontent.com:8080/u/r/main/SKILL.md",
    ],
)
@pytest.mark.asyncio
async def test_skill_install_rejects_non_github(url: str, monkeypatch) -> None:
    """Отказ ДО сети: httpx вообще не должен подниматься."""
    import httpx

    from app.skills import store

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError(f"сеть не должна дёргаться для {url}")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    with pytest.raises(ValueError):
        await store.fetch_skill_from_github(url)


@pytest.mark.asyncio
async def test_skill_install_allows_github(monkeypatch) -> None:
    """Нормальная github-ссылка проходит; ходим только на raw.githubusercontent."""
    from app.skills import store

    seen: list[str] = []

    async def fake_fetch(_client: object, url: str) -> str | None:
        seen.append(url)
        if url.endswith("main/SKILL.md"):
            return "# Тестовый навык\nделай хорошо"
        return None

    class _FakeClient:
        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(store, "_fetch_text", fake_fetch)
    monkeypatch.setattr(store.httpx, "AsyncClient", lambda **_kw: _FakeClient())

    name, content, raw_url = await store.fetch_skill_from_github(
        "https://github.com/anthropics/skills"
    )
    assert name == "Тестовый навык"
    assert "делай хорошо" in content
    assert raw_url.startswith("https://raw.githubusercontent.com/anthropics/skills/")
    assert all(u.startswith("https://raw.githubusercontent.com/") for u in seen)


@pytest.mark.asyncio
async def test_skill_fetch_blocks_offsite_redirect(monkeypatch) -> None:
    """302 с raw.githubusercontent на чужой хост не должен отдать тело."""
    from app.skills import store

    class _Resp:
        status_code = 200
        text = "секрет из метаданных"
        url = "http://169.254.169.254/latest/meta-data/"

    class _Client:
        async def get(self, _url: str) -> _Resp:
            return _Resp()

    got = await store._fetch_text(
        _Client(), "https://raw.githubusercontent.com/u/r/main/SKILL.md"
    )
    assert got is None


# ── G. Полное отсутствие регресса для владельца ────────────────────────────


@pytest.mark.asyncio
async def test_owner_theme_page_untouched_by_member_rows(env) -> None:
    """Строка участника в user_settings не влияет на рендер владельца."""
    client, db, owner_user, member_user = env
    await set_user_kv(db, member_user["id"], "theme", "light")
    templates_engine.invalidate_user_kv_sync(member_user["id"], "theme")

    await _as(client, owner_user["id"])
    body = (await client.get("/settings/theme")).text
    assert "theme-cosmos" in body


# ── H. IDOR на настройках ЧУЖОЙ сессии чата ────────────────────────────────
#
# ``chat_session.id`` — сквозной автоинкремент, а ``_set_effort`` / ``_set_mode``
# пишут ГЛОБАЛЬНЫЕ строки ``chat_effort_<id>`` / ``chat_mode_<id>``. Роуты
# принимали любой id без проверки владения (в отличие от соседних /stop и
# /system-prompt), поэтому участник перебором id менял режим и «эффорт» сессий
# ВЛАДЕЛЬЦА: ``bypass`` снимает «спрашивай перед действием» в его системном
# промпте, ``deep`` поднимает бюджет ответа до 16k токенов на его ключе.


@pytest_asyncio.fixture
async def chat_env(db: aiosqlite.Connection):
    from fastapi import FastAPI

    from app.auth import current_user_required
    from app.web.routes import chat_sessions

    owner_user = await create_user("owner@idor.test", "Zq7-frost-lantern-91")
    member_user = await create_user("member@idor.test", "Kp4-velvet-harbour-38")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    _reset_caches()

    app = FastAPI()
    app.include_router(chat_sessions.router)

    async def _as_member() -> dict[str, object]:
        return {"user_id": member_user["id"], "id": 1}

    app.dependency_overrides[current_user_required] = _as_member
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db, owner_user, member_user
    finally:
        _reset_caches()


@pytest.mark.asyncio
async def test_member_cannot_set_mode_of_foreign_session(chat_env) -> None:
    from app.chat.sessions import create_session

    client, db, owner_user, member_user = chat_env
    owner_session = await create_session(owner_user["id"], title="сессия владельца")

    r = await client.post(
        f"/api/chat/sessions/{owner_session['id']}/mode", json={"mode": "bypass"}
    )
    assert r.status_code == 404
    assert (await get_kv(db, f"chat_mode_{owner_session['id']}")) is None

    # Своя сессия по-прежнему настраивается.
    own = await create_session(member_user["id"], title="своя")
    ok = await client.post(f"/api/chat/sessions/{own['id']}/mode", json={"mode": "plan"})
    assert ok.status_code == 200
    assert (await get_kv(db, f"chat_mode_{own['id']}")) == "plan"


@pytest.mark.asyncio
async def test_member_cannot_set_effort_of_foreign_session(chat_env) -> None:
    from app.chat.sessions import create_session

    client, db, owner_user, member_user = chat_env
    owner_session = await create_session(owner_user["id"], title="сессия владельца")

    r = await client.post(
        f"/api/chat/sessions/{owner_session['id']}/effort", json={"effort": "deep"}
    )
    assert r.status_code == 404
    assert (await get_kv(db, f"chat_effort_{owner_session['id']}")) is None

    own = await create_session(member_user["id"], title="своя")
    ok = await client.post(
        f"/api/chat/sessions/{own['id']}/effort", json={"effort": "deep"}
    )
    assert ok.status_code == 200
    assert (await get_kv(db, f"chat_effort_{own['id']}")) == "deep"
