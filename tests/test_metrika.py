"""Счётчик Яндекс.Метрики (id 111901324) — кто его получает, когда и сколько раз.

Инвариант №1 (новый, 25.08.2026 — согласие): счётчик включает **вебвизор**,
то есть запись сессии. По 152-ФЗ это обработка персональных данных и она
требует согласия ДО начала обработки. Поэтому без куки ``persona_consent=all``
в HTML не должно быть НИ ``tag.js``, НИ noscript-пикселя: раньше пиксель бил в
Яндекс безусловно и обойти его согласием было нельзя. С кукой — счётчик
появляется ровно один раз.

Инвариант №2 (прежний): партиал ``_metrika.html`` подключается в двух местах —
в ``base.html`` (оболочка кабинета) и поимённо в публичных standalone-шаблонах,
которые base.html НЕ наследуют. Двойного включения быть не должно.

Инвариант №3 (прежний): в оболочке кабинета счётчик получает ТОЛЬКО владелец
инстанса. На экране кабинета приватное (текст чата, память, заметки); писать
сессию участника в Яндекс нельзя ни при каком согласии, поэтому участнику
счётчик не отдаётся вообще — ни с кукой согласия, ни без неё.
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
LOADER = "/static/consent.js"
CONSENT_COOKIE = "persona_consent"

PUBLIC_PATHS = ["/", "/pricing", "/features", "/blog"]


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


# ── Рубеж согласия ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", PUBLIC_PATHS)
async def test_public_pages_carry_no_counter_without_consent(
    client: AsyncClient, path: str
) -> None:
    """Нет согласия — нет ни скрипта, ни пикселя: запроса к Яндексу не будет."""
    response = await client.get(path, follow_redirects=True)
    assert response.status_code == 200, path
    body = response.text
    assert TAG not in body, path
    assert NOSCRIPT not in body, path
    # …но загрузчик согласия на странице есть — иначе баннер не покажется.
    assert LOADER in body, path


@pytest.mark.parametrize("path", PUBLIC_PATHS)
async def test_public_pages_carry_the_counter_once_after_consent(
    client: AsyncClient, path: str
) -> None:
    """С кукой ``persona_consent=all`` счётчик появляется ровно один раз."""
    client.cookies.set(CONSENT_COOKIE, "all")
    response = await client.get(path, follow_redirects=True)
    assert response.status_code == 200, path
    body = response.text
    assert body.count(TAG) == 1, (path, body.count(TAG))
    assert body.count(NOSCRIPT) == 1, path


@pytest.mark.parametrize("path", PUBLIC_PATHS)
async def test_explicit_rejection_keeps_the_counter_away(
    client: AsyncClient, path: str
) -> None:
    """«Только необходимые» — счётчика нет и баннер больше не всплывает."""
    client.cookies.set(CONSENT_COOKIE, "necessary")
    response = await client.get(path, follow_redirects=True)
    assert response.status_code == 200, path
    assert TAG not in response.text, path
    assert NOSCRIPT not in response.text, path


async def test_a_forged_consent_value_is_not_accepted(client: AsyncClient) -> None:
    """Признаётся ровно ``all``; любое другое значение = согласия нет."""
    for bogus in ("true", "1", "yes", "ALL", "all-of-it"):
        client.cookies.clear()
        client.cookies.set(CONSENT_COOKIE, bogus)
        body = (await client.get("/landing", follow_redirects=True)).text
        assert TAG not in body, bogus
        assert NOSCRIPT not in body, bogus


# ── Кабинет: владелец vs участник ───────────────────────────────────────────


async def test_owner_app_shell_carries_the_counter_once_after_consent(
    client: AsyncClient,
) -> None:
    token, _ = await issue_session(client._persona_owner_id)  # type: ignore[attr-defined]
    client.cookies.set(SESSION_COOKIE_NAME, token)
    client.cookies.set(CONSENT_COOKIE, "all")

    response = await client.get("/chat", follow_redirects=True)
    assert response.status_code == 200
    body = response.text
    assert body.count(TAG) == 1, body.count(TAG)
    assert body.count(NOSCRIPT) == 1


async def test_owner_app_shell_waits_for_consent_too(client: AsyncClient) -> None:
    """Владелец тоже человек: без согласия его сессию вебвизор не пишет."""
    token, _ = await issue_session(client._persona_owner_id)  # type: ignore[attr-defined]
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = await client.get("/chat", follow_redirects=True)
    assert response.status_code == 200
    assert TAG not in response.text
    assert NOSCRIPT not in response.text


async def test_member_app_shell_carries_no_counter_even_with_consent(
    client: AsyncClient,
) -> None:
    """Участник не получает счётчик НИКОГДА — даже нажав «Принять».

    Согласие снимает запрет на аналитику публичной воронки, но не превращает
    чужую переписку в материал для вебвизора.
    """
    member = await create_user("member@metrika.test", "Kp4-velvet-harbour-38")
    token, _ = await issue_session(member["id"])
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, token)
    client.cookies.set(CONSENT_COOKIE, "all")

    response = await client.get("/chat", follow_redirects=True)
    assert response.status_code == 200
    body = response.text
    assert TAG not in body
    assert NOSCRIPT not in body
    # и это именно оболочка кабинета, а не редирект на лендинг
    assert "/static/persona_theme.css" in body


@pytest.mark.parametrize("path", ["/landing", "/pricing", "/features", "/blog"])
async def test_public_pages_keep_the_counter_for_a_member_who_consented(
    client: AsyncClient, path: str
) -> None:
    """Маркетинговая воронка остаётся под аналитикой и для участника — с его согласия.

    ``/`` сюда не входит намеренно: залогиненного оно уводит в кабинет, а там
    счётчика для участника быть и не должно.
    """
    member = await create_user("public@metrika.test", "Kp4-velvet-harbour-38")
    token, _ = await issue_session(member["id"])
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, token)
    client.cookies.set(CONSENT_COOKIE, "all")

    response = await client.get(path, follow_redirects=True)
    assert response.status_code == 200, path
    assert response.text.count(TAG) == 1, path


# ── Статика загрузчика ──────────────────────────────────────────────────────


def test_loader_never_hardcodes_the_counter_into_markup() -> None:
    """Адрес Яндекса живёт в /static/consent.js, а не в шаблоне.

    Если бы URL счётчика лежал прямо в HTML, «отсутствие Метрики до согласия»
    проверялось бы по строке, которую легко вернуть обратно незаметно.
    """
    tpl = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "_metrika.html"
    text = tpl.read_text(encoding="utf-8")
    # Счётчик в шаблоне есть, но ТОЛЬКО внутри проверки куки.
    assert "persona_consent" in text
    assert text.index("persona_consent") < text.index(TAG)

    js = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "consent.js"
    body = js.read_text(encoding="utf-8")
    assert "Принять" in body
    assert "Только необходимые" in body


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
