"""Счётчик Яндекс.Метрики (id 111901324) — кто его получает и сколько раз.

Партиал ``_metrika.html`` подключается в двух местах: в ``base.html`` (оболочка
кабинета) и поимённо в публичных standalone-шаблонах, которые base.html НЕ
наследуют. Двойного включения быть не должно — иначе Метрика посчитает визит
дважды, а webvisor запишет сессию два раза.

Второй, более важный инвариант: в оболочке кабинета счётчик получает ТОЛЬКО
владелец инстанса. Партиал включает ``webvisor`` — полную запись экрана, а на
экране кабинета приватное (текст чата, память, заметки). Писать сессию
участника в Яндекс без его согласия нельзя, поэтому участнику счётчик не
отдаётся вообще. Публичные маркетинговые страницы остаются под аналитикой для
всех — это воронка, там приватных данных нет.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.web.main import create_app
from app.web.routes import setup_gate

TAG = "mc.yandex.ru/metrika/tag.js?id=111901324"
NOSCRIPT = "mc.yandex.ru/watch/111901324"


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    await init_database()
    owner_user = await create_user("owner@metrika.test", "Zq7-frost-lantern-91")
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner_user["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
    setup_gate._cache.mark_done()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac._persona_owner_id = owner_user["id"]  # type: ignore[attr-defined]
        yield ac


@pytest.mark.parametrize("path", ["/", "/pricing", "/features", "/blog"])
async def test_public_pages_carry_the_counter_once(client: AsyncClient, path: str) -> None:
    response = await client.get(path, follow_redirects=True)
    assert response.status_code == 200, path
    body = response.text
    assert body.count(TAG) == 1, (path, body.count(TAG))
    assert body.count(NOSCRIPT) == 1, path


async def test_owner_app_shell_carries_the_counter_once(client: AsyncClient) -> None:
    token, _ = await issue_session(client._persona_owner_id)  # type: ignore[attr-defined]
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = await client.get("/chat", follow_redirects=True)
    assert response.status_code == 200
    body = response.text
    assert body.count(TAG) == 1, body.count(TAG)
    assert body.count(NOSCRIPT) == 1


async def test_member_app_shell_carries_no_counter_at_all(client: AsyncClient) -> None:
    """Участник не получает НИ tag.js, НИ noscript-пиксель — вебвизор молчит."""
    member = await create_user("member@metrika.test", "Kp4-velvet-harbour-38")
    token, _ = await issue_session(member["id"])
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = await client.get("/chat", follow_redirects=True)
    assert response.status_code == 200
    body = response.text
    assert TAG not in body
    assert NOSCRIPT not in body
    # и это именно оболочка кабинета, а не редирект на лендинг
    assert "/static/persona_theme.css" in body


@pytest.mark.parametrize("path", ["/landing", "/pricing", "/features", "/blog"])
async def test_public_pages_keep_the_counter_for_a_member(
    client: AsyncClient, path: str
) -> None:
    """Маркетинговая воронка остаётся под аналитикой даже для участника.

    ``/`` сюда не входит намеренно: залогиненного оно уводит в кабинет, а там
    счётчика для участника быть и не должно.
    """
    member = await create_user("public@metrika.test", "Kp4-velvet-harbour-38")
    token, _ = await issue_session(member["id"])
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = await client.get(path, follow_redirects=True)
    assert response.status_code == 200, path
    assert response.text.count(TAG) == 1, path


def test_partial_is_not_included_by_templates_extending_base() -> None:
    """base.html уже несёт счётчик — потомки не должны его дублировать."""
    tpl_dir = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    offenders = []
    for path in tpl_dir.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        if "_metrika.html" not in text:
            continue
        if path.name == "_metrika.html":
            continue
        if '{% extends "base.html" %}' in text:
            offenders.append(path.name)
    assert offenders == [], offenders
