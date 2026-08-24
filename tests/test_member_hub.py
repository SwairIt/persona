"""Хаб настроек и навбар глазами УЧАСТНИКА (зарегистрированный не-владелец).

Контракт:
  * ``/settings/hub`` участнику отдаёт _MEMBER_CATEGORIES — только его
    собственные настройки. Ни «Захват», ни «OCR», ни диагностика туда не
    попадают даже как строка в разметке.
  * ``/api/settings/search`` участнику ищет по тому же урезанному каталогу,
    поэтому owner-only страницу нельзя нащупать поиском (у владельца — можно).
  * ``base.html`` определяет роль по ``request.state.is_owner`` (его кладёт
    auth-гейт для любого аутентифицированного запроса), а НЕ по тому, вспомнил
    ли роут положить ``is_owner`` в контекст. Забывчивый роут больше не
    показывает участнику owner-навбар.

Титулы категорий уезжают в разметку через Jinja ``|tojson``, а он по умолчанию
``ensure_ascii=True`` — кириллица становится ``\\uXXXX``. Поэтому проверяем
через :func:`_contains`, которое сравнивает и с сырой строкой, и с её
json-экранированной формой.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.repository import set_kv
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware
from app.web.routes import settings_hub as hub
from app.web.templates_engine import templates

# Заглушка страницы, которая НЕ передаёт ``is_owner`` в контекст — ровно тот
# случай, ради которого base.html теперь смотрит в request.state.
_CHAT_STUB = templates.env.from_string(
    '{% extends "base.html" %}{% block content %}CHAT-STUB{% endblock %}'
)


def _contains(body: str, text: str) -> bool:
    """Есть ли ``text`` в разметке — хоть сырым, хоть json-экранированным."""
    return text in body or json.dumps(text, ensure_ascii=True)[1:-1] in body


def _reset_auth_caches() -> None:
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


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(hub.router)

    @app.get("/chat", response_class=HTMLResponse)
    async def _chat(request: Request) -> HTMLResponse:
        return HTMLResponse(
            _CHAT_STUB.render({"request": request, "title": "Chat", "active_nav": "chat"})
        )

    return app


@pytest_asyncio.fixture
async def hub_setup(db: aiosqlite.Connection):
    owner_user = await create_user("owner@hub.test", "owner-pass-123")
    member_user = await create_user("member@hub.test", "member-pass-123")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    # Копия, которую проверяем ниже, — русская (язык интерфейса по умолчанию
    # в тестах — en, иначе сверялись бы с EN-переводом).
    await set_kv(db, "ui_language", "ru")
    i18n.invalidate_language_cache()
    _reset_auth_caches()

    transport = ASGITransport(app=_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, owner_user, member_user
    finally:
        _reset_auth_caches()
        i18n.invalidate_language_cache()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


# ── Каталог ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_hub_shows_only_member_categories(hub_setup):
    client, _owner_user, member_user = hub_setup
    await _as(client, member_user["id"])

    r = await client.get("/settings/hub", follow_redirects=False)
    assert r.status_code == 200
    body = r.text

    assert _contains(body, "Мой ИИ")
    assert _contains(body, "Что ИИ помнит о тебе")
    # ...и ничего из поверхности владельца
    assert not _contains(body, "Захват")
    assert not _contains(body, "OCR и распознавание")
    assert not _contains(body, "Диагностика")
    assert "/settings/capture" not in body
    assert "/ocr-admin" not in body
    assert "/root" not in body
    assert "/admin/" not in body
    # экспорт/импорт профиля настроек — owner-only бэкенд, кнопок нет
    assert "/api/settings/profile/export.json" not in body


@pytest.mark.asyncio
async def test_member_hub_lists_exactly_the_shipped_pages(hub_setup):
    client, _owner_user, member_user = hub_setup
    await _as(client, member_user["id"])
    assert (await client.get("/settings/hub")).status_code == 200

    hrefs = [
        p["href"]
        for cat in hub._categories_json(member=True)
        for p in cat["pages"]  # type: ignore[index]
    ]
    assert hrefs == [
        "/settings/llm",
        "/settings/system-prompt",
        "/settings/advanced",
        "/settings/skills",
        "/settings/memory",
        "/graph",
        "/settings/profile",
        "/settings/theme",
        "/auth/set-password",
        "/auth/logout",
    ]


def test_member_catalogue_never_leaves_the_free_surface():
    """Каждая member-ссылка либо открыта гейтом, либо это /auth/* (public)."""
    for cat in hub._MEMBER_CATEGORIES:
        for href, _label in cat["pages"]:  # type: ignore[union-attr]
            assert href.startswith("/auth/") or auth_gate._is_member_path(href), href


@pytest.mark.asyncio
async def test_owner_hub_keeps_all_categories_plus_essentials(hub_setup):
    client, owner_user, _member_user = hub_setup
    await _as(client, owner_user["id"])

    r = await client.get("/settings/hub", follow_redirects=False)
    assert r.status_code == 200
    body = r.text

    for title in (
        "Основное",
        "Захват",
        "OCR и распознавание",
        "Память и сводки",
        "AI, чат и инструменты",
        "Устройства и синхронизация",
        "Приложения и теги",
        "Уведомления и интеграции",
        "Внешний вид",
        "Безопасность и обслуживание",
        "Диагностика",
    ):
        assert _contains(body, title), title

    titles = [c["title"] for c in hub._categories_json(lang="ru")]
    assert titles[0] == "Основное"
    # владелец видит блок экспорта профиля настроек
    assert "/api/settings/profile/export.json" in body


def test_advanced_categories_are_flagged_but_still_present():
    cats = hub._categories_json(lang="ru")
    advanced = {c["title"] for c in cats if c["advanced"]}
    assert advanced == {
        "OCR и распознавание",
        "Приложения и теги",
        "Уведомления и интеграции",
        "Диагностика",
    }
    # ни одна страница не потерялась при перегруппировке
    hrefs = {p["href"] for c in cats for p in c["pages"]}  # type: ignore[index]
    for href in ("/ocr-admin", "/webhooks", "/doctor", "/settings/tag-aliases"):
        assert href in hrefs


# ── Поиск ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_search_cannot_reach_owner_pages(hub_setup):
    client, owner_user, member_user = hub_setup

    await _as(client, member_user["id"])
    member_hits = (await client.get("/api/settings/search", params={"q": "захват"})).json()
    assert member_hits["results"] == []

    await _as(client, owner_user["id"])
    owner_hits = (await client.get("/api/settings/search", params={"q": "захват"})).json()
    assert owner_hits["results"], "владелец обязан находить страницы захвата"
    assert any(r["href"] == "/settings/capture" for r in owner_hits["results"])


@pytest.mark.asyncio
async def test_member_search_only_returns_member_hrefs(hub_setup):
    client, _owner_user, member_user = hub_setup
    await _as(client, member_user["id"])

    allowed = {
        href
        for cat in hub._MEMBER_CATEGORIES
        for href, _label in cat["pages"]  # type: ignore[union-attr]
    }
    for query in ("память", "модель", "тема", "пароль", "a", "/"):
        results = (
            await client.get("/api/settings/search", params={"q": query})
        ).json()["results"]
        for row in results:
            assert row["href"] in allowed, (query, row["href"])


def test_search_deduplicates_pages_shared_between_categories():
    """«Основное» дублирует популярные страницы — поиск не должен двоить."""
    hits = hub.search_settings("тема")
    hrefs = [h["href"] for h in hits]
    assert "/settings/theme" in hrefs
    assert len(hrefs) == len(set(hrefs))


# ── Навбар (base.html) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_nav_is_rendered_without_route_passing_is_owner(hub_setup):
    """Роут-заглушка не передаёт ``is_owner`` — роль берётся из request.state."""
    client, _owner_user, member_user = hub_setup
    await _as(client, member_user["id"])

    body = (await client.get("/chat", follow_redirects=False)).text
    assert "CHAT-STUB" in body

    # member core + more
    for href in ("/settings/memory", "/graph", "/voice", "/settings/hub",
                 "/settings/skills", "/settings/system-prompt", "/settings/profile"):
        assert href in body, href
    assert "Навыки" in body  # nav_skills в more-меню

    # ничего из owner-навбара и захват-пилюли
    # ``captureStatus()``/``micToggle()`` остаются определены в inline-скрипте
    # (там же живёт переключатель темы) — сторожим ИМЕННО разметку.
    for marker in ('href="/now"', 'href="/timeline"', 'href="/root"',
                   'href="/m"', 'href="/ask"', '@click="captureNow()"',
                   'x-data="micToggle()"', 'id="status-pill-heartbeat-dot"'):
        assert marker not in body, marker
    # аккаунт-чип получает member-ветку
    assert 'x-if="!isOwner"' in body


@pytest.mark.asyncio
async def test_owner_nav_stays_intact(hub_setup):
    client, owner_user, _member_user = hub_setup
    await _as(client, owner_user["id"])

    body = (await client.get("/chat", follow_redirects=False)).text
    for marker in ('href="/now"', 'href="/timeline"', 'href="/root"',
                   'href="/m"', '@click="captureNow()"', 'x-data="micToggle()"',
                   'id="status-pill-heartbeat-dot"', 'href="/billing"'):
        assert marker in body, marker
