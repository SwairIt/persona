"""Публичные страницы MVP «бесплатно со своим ключом».

Проверяем, что гость (без сессии) реально видит:
  * /help/connect-llm — гайд «подключи свою модель» (новая страница);
  * /pricing         — витрину с бесплатным тарифом, без Pro-690 ₽;
  * /                — лендинг с оффером «бесплатно со своим ключом».

Все три лежат под публичными префиксами auth-гейта, поэтому 200 без cookie —
это часть контракта, а не деталь реализации: на /help/connect-llm ссылаются
и лендинг, и /pricing.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.web.main import create_app
from app.web.routes import setup_gate


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    await init_database()
    # Свежая тестовая БД = «мастер настройки не пройден», и setup-гейт
    # завернул бы даже публичные страницы на /setup. Помечаем установку
    # завершённой, чтобы проверять именно маркетинговые роуты.
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
    setup_gate._cache.mark_done()

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_connect_llm_guide_is_public(client: AsyncClient) -> None:
    response = await client.get("/help/connect-llm")

    assert response.status_code == 200
    body = response.text
    # Каждый провайдер из гайда должен быть на странице.
    for provider in ("OpenRouter", "Groq", "DeepSeek", "Ollama", "ProxyAPI", "AITunnel"):
        assert provider in body, provider
    # Ключевые ссылки: куда идти подключать и где регистрироваться.
    assert "/settings/llm" in body
    assert "/auth/signup" in body


async def test_pricing_sells_free_tier_not_pro(client: AsyncClient) -> None:
    response = await client.get("/pricing")

    assert response.status_code == 200
    body = response.text
    assert "0&nbsp;₽" in body
    assert "Бесплатно" in body
    assert "/help/connect-llm" in body
    # Pro-карточка закомментирована на время MVP — рендериться не должна.
    assert "690" not in body
    assert "Оформить Pro" not in body
    # Self-host остаётся.
    assert "Self-host" in body


@pytest.mark.parametrize("path", ["/", "/landing"])
async def test_landing_promises_free_with_own_key(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    body = response.text
    assert "Бесплатно — со своим ключом" in body
    # Платного триала на лендинге больше нет.
    assert "3 дня Pro" not in body
    assert "690" not in body
