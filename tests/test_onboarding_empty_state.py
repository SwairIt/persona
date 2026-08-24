"""Онбординг MVP «бесплатно со своим ключом».

Два уровня:

* ``/onboarding`` — страница участника: биллинга/триала на ней больше НЕТ,
  главный шаг — подключить свою LLM (``/settings/llm`` + гайд
  ``/help/connect-llm``), а кнопка «Открыть чат» по-прежнему ставит флаг
  ``onboarded_<uid>`` и уводит в чат.
* ``chat_index.html`` — пустой экран чата: примеры-кейсы, сид черновика (S2c)
  и плашка «нет модели», которая участнику обязана вести к СВОЕМУ ключу,
  а не обещать, что владелец скоро всё починит.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import current_user_required
from app.auth import owner as owner_mod
from app.auth.users import create_user
from app.storage.repository import get_kv, set_kv, set_user_kv
from app.web.routes import onboarding
from app.web.templates_engine import templates

# --- chat_index.html: пустой экран -----------------------------------------


def _render_empty(*, llm_configured: bool = True, is_owner: bool = False) -> str:
    t = templates.env.get_template("chat_index.html")
    return t.render(
        request=None,
        app_version="test",
        title="t",
        active_nav="chat",
        sessions=[],
        active_session=None,
        messages=[],
        adv={},
        provider_badge={"provider": "ollama", "is_local": True},
        llm_configured=llm_configured,
        is_owner=is_owner,
        lang="ru",
        t=lambda *a, **k: (a[0] if a else ""),
    )


def test_empty_state_has_onboarding_cases() -> None:
    html = _render_empty()
    assert "onboardCases" in html  # массив примеров в Alpine
    assert "newChatWith" in html  # клик создаёт чат с текстом
    assert "?draft=" in html  # сид черновика в URL
    assert "пустой чат" in html  # запасной выход


def test_empty_state_shows_privacy_badge_context() -> None:
    # На пустом экране бейдж в шапке не рисуется (нет active_session),
    # но провайдер всё равно прокинут без падений рендера.
    html = _render_empty()
    assert "Чат с памятью" in html


def test_member_without_llm_sees_connect_your_provider_banner() -> None:
    """Первый визит участника: плашка ведёт к своему ключу, а не «жди»."""
    html = _render_empty(llm_configured=False, is_owner=False)
    assert "Подключи свой AI-провайдер" in html
    assert "/settings/llm" in html
    assert "/help/connect-llm" in html
    # Owner-формулировка «модель офлайн, я скоро вернусь» участнику не нужна.
    assert "пока офлайн" not in html


def test_owner_without_llm_keeps_offline_copy() -> None:
    html = _render_empty(llm_configured=False, is_owner=True)
    assert "пока офлайн" in html
    assert "/settings/llm" in html


# --- /onboarding: страница участника ---------------------------------------


@pytest_asyncio.fixture
async def member(db: aiosqlite.Connection) -> int:
    """Владелец (первый id) + обычный участник, который и проходит онбординг."""
    owner = await create_user("owner@example.test", "owner-pass-123")
    user = await create_user("member@example.test", "member-pass-123")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0
    return int(user["id"])


@pytest_asyncio.fixture
async def client(member: int) -> AsyncClient:
    app = FastAPI()
    app.include_router(onboarding.router)

    async def _fake_session() -> dict[str, object]:
        return {"user_id": member, "id": 1, "email": "member@example.test"}

    app.dependency_overrides[current_user_required] = _fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_onboarding_has_no_billing_left(client: AsyncClient) -> None:
    """Ни цен, ни триала, ни чекаута — биллинг на MVP спит."""
    response = await client.get("/onboarding")

    assert response.status_code == 200, response.text
    body = response.text
    for dead in ("690", "5900", "Триал", "триал", "/billing/checkout"):
        assert dead not in body, dead
    # Хвост старой страницы: подвал вёл в раздел подписки.
    assert "Управлять подпиской" not in body
    # (ссылка /billing в общем навбаре — не эта страница, её трогает другой агент)


@pytest.mark.asyncio
async def test_onboarding_points_to_own_llm(client: AsyncClient) -> None:
    """Шаг 2 — подключить свою модель, с гайдом «где взять ключ»."""
    response = await client.get("/onboarding")

    body = response.text
    assert "/settings/llm" in body
    assert "/help/connect-llm" in body
    assert "Подключи свою LLM" in body
    assert "Подключить модель" in body
    # Старое обещание «работает на нашей модели» — неправда, его быть не должно.
    assert "на нашей модели" not in body
    # Шаг 3 доступен и без ключа: у чата своя плашка.
    assert 'action="/onboarding/complete"' in body


@pytest.mark.asyncio
async def test_onboarding_shows_ready_state_when_llm_configured(
    db: aiosqlite.Connection, member: int, client: AsyncClient
) -> None:
    """С настроенным провайдером — «модель подключена», без утечки ключа."""
    await set_user_kv(db, member, "llm_provider", "openrouter")
    await set_user_kv(db, member, "byo_api_key_openrouter", "sk-or-MEMBER-SECRET")

    response = await client.get("/onboarding")

    body = response.text
    assert "Модель подключена" in body
    assert "Подключить модель" not in body  # CTA «подключи» уже не нужна
    assert "sk-or-MEMBER-SECRET" not in body


@pytest.mark.asyncio
async def test_onboarding_complete_sets_flag_and_redirects(
    db: aiosqlite.Connection, member: int, client: AsyncClient
) -> None:
    response = await client.post("/onboarding/complete")

    assert response.status_code == 303
    assert response.headers["location"] == "/chat"
    assert await get_kv(db, f"onboarded_{member}") == "1"
