"""Первосторонняя аналитика владельца — инварианты, которые нельзя сломать молча.

Эта поверхность добавляет поведенческий сбор о живых людях на сайт, который на
прошлой неделе опубликовал политику по 152-ФЗ. Поэтому тесты здесь сторожат не
«красиво ли считается», а ровно четыре вещи:

1. **Приватность.** В таблице событий не должно оказаться ни строки
   user-agent, ни IP, ни полного реферера — даже если они пришли в запросе.
   Регресс тут не падает и не логируется: он просто тихо начинает собирать то,
   чего мы не обещали.
2. **Правильность цифр.** Свёртка суток обязана совпадать с числом, посчитанным
   руками, а воронка — с заранее разложенной по ступеням выборкой людей.
   Дашборд, который врёт, хуже, чем его отсутствие: по нему принимают решения.
3. **Изоляция.** Дашборд — владельческий. Участник не должен ни открыть его, ни
   увидеть ссылку на ``/root`` в своей разметке (ссылка в DOM — это ссылка:
   её видит поиск по странице, «открыть в новой вкладке» и краулер).
4. **Безвредность.** Отказ аналитики не имеет права уронить запрос, который она
   считала, а выключатель обязан выключать.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.analytics import capture, funnel, store
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection
from app.storage.repository import set_kv, set_user_kv
from app.web.main import create_app
from app.web.middleware import analytics as analytics_mw
from app.web.middleware import auth_gate
from app.web.routes import setup_gate

OWNER_PW = "Zq7-frost-lantern-91"
MEMBER_PW = "Kp4-velvet-harbour-38"


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


@pytest.fixture(autouse=True)
def _clean_capture_state():
    """Буфер и кэш рубильника — процессные глобалы; чистим до и после каждого теста."""
    capture.reset_buffer()
    capture.reset_cache()
    analytics_mw.reset_seen()
    yield
    capture.reset_buffer()
    capture.reset_cache()
    analytics_mw.reset_seen()


@pytest_asyncio.fixture
async def site(db: aiosqlite.Connection):
    """Полное приложение с владельцем и участником — как в проде, а не заглушки."""
    owner_user = await create_user("owner@analytics.test", OWNER_PW)
    member_user = await create_user("member@analytics.test", MEMBER_PW)
    await set_kv(db, "setup_complete", "true")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    await db.commit()
    setup_gate._cache.mark_done()
    _reset_auth_caches()
    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, owner_user, member_user
    finally:
        _reset_auth_caches()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


async def _rows(sql: str, params: tuple = ()) -> list[tuple]:
    """Строки как ОБЫЧНЫЕ кортежи: ``aiosqlite.Row`` не равен tuple, и сравнение
    с фикстурой молча оказалось бы False при полностью верных данных."""
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        return [tuple(row) for row in await cur.fetchall()]


# ── 1. ПРИВАТНОСТЬ ───────────────────────────────────────────────────────────


async def test_view_is_recorded_with_the_normalised_route_path(site) -> None:
    """``/day/2026-08-25`` схлопывается в шаблон роута, а не в сто разных строк.

    Нормализация идёт по таблице роутов приложения. Регулярка «замени цифры на
    {id}» тут сломалась бы: параметр — дата, а не число.
    """
    client, owner_user, _member = site
    await _as(client, owner_user["id"])
    await client.get("/day/2026-08-25", follow_redirects=False)
    await capture.flush()

    paths = [r[0] for r in await _rows("SELECT path FROM analytics_event")]
    assert paths, "просмотр не записался вовсе"
    assert "/day/2026-08-25" not in paths, "сырой путь с датой попал в таблицу"
    assert any("{" in p for p in paths), f"путь не свёрнут к шаблону роута: {paths}"


async def test_no_user_agent_no_ip_and_no_full_referrer_is_stored(site) -> None:
    """Приходит всё — в базу не попадает ничего из этого.

    Запрос несёт узнаваемый user-agent, реферер с поисковым запросом в query и
    (через ASGI-транспорт) адрес клиента. В таблице должны остаться только
    класс устройства и ХОСТ реферера.
    """
    client, owner_user, _member = site
    await _as(client, owner_user["id"])
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4) AppleWebKit/605.1.15 Secret/9"
    # Заголовки — только ASCII: httpx (как и HTTP/1.1) кириллицу в них не
    # пропускает. Поисковый запрос имитируем узнаваемым латинским маркером.
    await client.get(
        "/chat",
        headers={
            "user-agent": ua,
            "referer": "https://ya.ru/search/?text=private-search-marker&uid=42",
        },
        follow_redirects=False,
    )
    await capture.flush()

    async with get_connection() as conn:
        cur = await conn.execute("SELECT * FROM analytics_event")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]
    assert rows, "просмотр не записался"

    # Ни одной колонки под UA/IP в схеме вообще нет — это первая линия защиты.
    assert "user_agent" not in cols and "ip" not in cols and "ip_hash" not in cols

    blob = " ".join(str(v) for row in rows for v in row.values())
    assert "Mozilla" not in blob and "AppleWebKit" not in blob and "Secret" not in blob
    assert "search" not in blob and "text=" not in blob
    assert "private-search-marker" not in blob and "uid=42" not in blob
    assert "127.0.0.1" not in blob and "testclient" not in blob

    row = rows[0]
    assert row["device"] == "mobile", "класс устройства всё-таки нужен"
    assert row["referrer_host"] == "ya.ru", "хост реферера — единственное, что берём"


def test_referrer_host_drops_everything_except_the_host() -> None:
    assert capture.referrer_host("https://ya.ru/search?text=тайна#frag") == "ya.ru"
    assert capture.referrer_host("http://user:pw@example.com:8443/a/b") == "example.com"
    assert capture.referrer_host("") == ""
    # Мусор без точки хостом не считаем — иначе в колонку источников попадёт
    # что угодно, что браузер положил в Referer.
    assert capture.referrer_host("about:blank") == ""


def test_device_class_never_returns_the_raw_string() -> None:
    assert capture.device_class("Mozilla/5.0 (Windows NT 10.0) Chrome/120") == "desktop"
    assert capture.device_class("Mozilla/5.0 (iPhone) Mobile/15E148") == "mobile"
    assert capture.device_class("Googlebot/2.1 (+http://www.google.com/bot.html)") == "bot"
    assert capture.device_class("") == "unknown"


def test_anonymous_without_consent_gets_no_linkable_pseudonym() -> None:
    """Решение про согласие, выраженное кодом: без «Принять» визиты не склеиваются."""
    common = dict(salt="s" * 32, device="desktop", day="2026-08-25")
    assert (
        capture.session_pseudonym(
            session_token=None,
            client_ip="203.0.113.7",
            consented=False,
            authenticated=False,
            **common,
        )
        is None
    )
    with_consent = capture.session_pseudonym(
        session_token=None,
        client_ip="203.0.113.7",
        consented=True,
        authenticated=False,
        **common,
    )
    assert with_consent and "203.0.113.7" not in with_consent
    # Пересолка сутками: завтра тот же посетитель — уже другой псевдоним.
    tomorrow = capture.session_pseudonym(
        salt="s" * 32,
        device="desktop",
        day="2026-08-26",
        session_token=None,
        client_ip="203.0.113.7",
        consented=True,
        authenticated=False,
    )
    assert tomorrow != with_consent


# ── 2. ПРАВИЛЬНОСТЬ ЦИФР ─────────────────────────────────────────────────────


async def test_rollup_matches_a_hand_computed_fixture(db) -> None:
    """Свёртка = ровно те числа, которые можно посчитать пальцем.

    Раскладка: 2026-01-05 — три просмотра ``/chat`` владельцем и один
    участником; 2026-01-06 — два просмотра ``/chat`` владельцем. Сегодня по
    условию 2026-01-07, значит обе даты ЗАКРЫТЫ и обе должны свернуться.
    """
    events = []
    for _ in range(3):
        events.append(_ev("2026-01-05", "/chat", "owner", "sess-o"))
    events.append(_ev("2026-01-05", "/chat", "member", "sess-m"))
    for _ in range(2):
        events.append(_ev("2026-01-06", "/chat", "owner", "sess-o"))
    await store.insert_events(events)

    rolled = await store.rollup_closed_days(today="2026-01-07")
    assert rolled == ["2026-01-05", "2026-01-06"]

    daily = await _rows(
        "SELECT day, role, hits FROM analytics_daily ORDER BY day, role"
    )
    assert daily == [
        ("2026-01-05", "member", 1),
        ("2026-01-05", "owner", 3),
        ("2026-01-06", "owner", 2),
    ]

    uniq = await _rows(
        "SELECT day, role, sessions FROM analytics_daily_unique ORDER BY day, role"
    )
    assert uniq == [
        ("2026-01-05", "member", 1),
        ("2026-01-05", "owner", 1),  # три хита одной сессии — это одна сессия
        ("2026-01-06", "owner", 1),
    ]

    # Повторный вызов идемпотентен: два воркера открыли дашборд одновременно.
    assert await store.rollup_closed_days(today="2026-01-07") == []
    again = await _rows("SELECT SUM(hits) FROM analytics_daily")
    assert again == [(6,)]

    # И главное: чтение поверх свёртки даёт те же 6 хитов, что и сырьё.
    agg = await store.aggregate("2026-01-01", "2026-01-07", group=("path",))
    assert agg == [{"path": "/chat", "hits": 6}]


async def test_aggregate_merges_rolled_days_with_the_unrolled_tail(db) -> None:
    """Закрытые сутки читаются из свёртки, сегодняшние — из сырья, сумма одна."""
    await store.insert_events(
        [_ev("2026-01-05", "/chat", "owner", "s1"), _ev("2026-01-06", "/chat", "owner", "s1")]
    )
    await store.rollup_closed_days(today="2026-01-06")  # свернулся только 05

    assert await store.rollup_state() == "2026-01-05"
    agg = await store.aggregate("2026-01-01", "2026-01-06", group=("path",))
    assert agg == [{"path": "/chat", "hits": 2}], "хвост потерялся или посчитался дважды"


async def test_purge_deletes_only_rows_outside_the_window(db) -> None:
    """Окно ВКЛЮЧАЮЩЕЕ: при 3 сутках остаются ровно три последних дня."""
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    await store.insert_events([_ev(d, "/chat", "owner", "s1") for d in days])

    removed = await store.purge_old_events(3, today="2026-01-09")
    assert removed == 2

    left = sorted({r[0] for r in await _rows("SELECT day FROM analytics_event")})
    assert left == ["2026-01-07", "2026-01-08", "2026-01-09"]


async def test_purge_keeps_the_rolled_up_counters(db) -> None:
    """Вычистка сырья не трогает свёртку: в ней нет идентификаторов, терять нечего."""
    await store.insert_events([_ev("2026-01-01", "/chat", "owner", "s1")])
    await store.rollup_closed_days(today="2026-01-09")
    await store.purge_old_events(3, today="2026-01-09")

    assert await _rows("SELECT day FROM analytics_event") == []
    assert await _rows("SELECT day, hits FROM analytics_daily") == [("2026-01-01", 1)]


async def test_funnel_numbers_are_right_for_seeded_users(db) -> None:
    """Четыре человека на четырёх разных ступенях — воронка обязана их различить.

    Ступени ниже «аккаунт создан» намеренно считаются по СУЩЕСТВУЮЩИМ таблицам
    (``users`` / kv / ``user_settings`` / ``chat_message``), поэтому этот тест
    не пишет ни одного события аналитики — и всё равно получает цифры.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    await create_user("a@funnel.test", OWNER_PW)               # только регистрация
    b = await create_user("b@funnel.test", MEMBER_PW)          # + онбординг
    c = await create_user("c@funnel.test", "Wm5-copper-meadow-77")   # + модель
    d = await create_user("d@funnel.test", "Rt8-silver-thicket-52")  # + сообщение

    async with get_connection() as conn:
        for uid in (b["id"], c["id"], d["id"]):
            await set_kv(conn, f"onboarded_{uid}", "1")
        for uid in (c["id"], d["id"]):
            await set_user_kv(conn, uid, "llm_provider", "ollama")
        await conn.execute(
            "INSERT INTO chat_session (id, user_id, title) VALUES (1, ?, 'x')",
            (d["id"],),
        )
        await conn.execute(
            "INSERT INTO chat_message (session_id, role, content) "
            "VALUES (1, 'user', 'привет')"
        )
        await conn.commit()

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    result = await funnel.build_funnel(today, today)
    counts = {s["key"]: s["count"] for s in result["stages"]}

    assert counts["registered"] == 4
    assert counts["onboarded"] == 3
    assert counts["llm"] == 2
    assert counts["first_message"] == 1

    stages = {s["key"]: s for s in result["stages"]}
    # Конверсия к предыдущей ступени: 3 из 4, 2 из 3, 1 из 2.
    assert stages["onboarded"]["step_pct"] == 75.0
    assert stages["llm"]["step_pct"] == pytest.approx(66.7)
    assert stages["first_message"]["step_pct"] == 50.0
    # Верхние две ступени честно помечены как неретроспективные.
    assert stages["landing"]["retroactive"] is False
    assert stages["registered"]["retroactive"] is True


# ── 3. ИЗОЛЯЦИЯ ──────────────────────────────────────────────────────────────


async def test_dashboard_renders_for_the_owner(site) -> None:
    client, owner_user, _member = site
    await _as(client, owner_user["id"])
    response = await client.get("/root/analytics", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "Аналитика сайта" in body
    # Честность про пробелы в данных — не украшение, а требование к странице.
    assert "Событий пока нет вообще" in body or "Данные событий — с" in body


async def test_dashboard_renders_every_populated_section(site) -> None:
    """С данными шаблон обязан пройти ВСЕ ветки, а не только «пусто».

    Пустой дашборд рендерится и в предыдущем тесте; здесь важно другое —
    заполненные таблицы, спарклайн, столбцы SVG и подписи источников. Это
    единственный способ поймать опечатку в шаблоне до продакшена: Jinja на
    неизвестном поле молча отдаёт пустоту, а не падает.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    client, owner_user, member_user = site
    today = datetime.now(UTC)
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    events = [
        _ev(yesterday, "/chat", "owner", "s-own"),
        _ev(yesterday, "/chat", "member", "s-mem"),
        _ev(yesterday, "/landing", "anonymous", "s-anon"),
    ]
    events[2]["referrer_host"] = "ya.ru"
    events[0]["user_id"] = owner_user["id"]
    events[1]["user_id"] = member_user["id"]
    events.append(
        {
            **_ev(yesterday, "/landing", "anonymous", "s-anon"),
            "kind": capture.KIND_CLICK,
            "label": "cta:signup",
        }
    )
    await store.insert_events(events)

    await _as(client, owner_user["id"])
    body = (await client.get("/root/analytics?days=30", follow_redirects=False)).text

    assert "Данные событий — с" in body, "рамка честности пропала"
    assert "/landing" in body and "/chat" in body      # таблица страниц
    assert "cta:signup" in body                         # таблица кликов
    assert "ya.ru" in body                              # источники
    assert "<polyline" in body and "<rect" in body      # оба инлайновых графика
    assert "Создали аккаунт" in body                    # воронка
    assert "users.created_at" in body                   # подпись источника ступени


async def test_dashboard_is_closed_for_member_and_anonymous(site) -> None:
    client, _owner_user, member_user = site

    await _as(client, member_user["id"])
    member = await client.get("/root/analytics", follow_redirects=False)
    assert member.status_code == 303
    assert member.headers["location"] == "/chat"

    client.cookies.clear()
    anon = await client.get("/root/analytics", follow_redirects=False)
    assert anon.status_code == 303
    assert anon.headers["location"] == "/landing"


async def test_member_html_contains_no_root_link_at_all(site) -> None:
    """Ссылки на ``/root`` не должно быть в разметке участника ФИЗИЧЕСКИ.

    Не «скрыта CSS» и не «погашена Alpine по ответу API»: ссылка в DOM — это
    рабочая ссылка для поиска по странице, «открыть в новой вкладке» и
    краулера. Условие в base.html — серверный ``{% if %}`` по роли из
    ``request.state.is_owner``.
    """
    client, owner_user, member_user = site

    await _as(client, member_user["id"])
    member_body = (await client.get("/chat", follow_redirects=False)).text
    assert 'href="/root' not in member_body
    assert "/root/analytics" not in member_body

    await _as(client, owner_user["id"])
    owner_body = (await client.get("/chat", follow_redirects=False)).text
    assert 'href="/root"' in owner_body
    assert 'href="/root/analytics"' in owner_body


async def test_settings_write_is_owner_only(site) -> None:
    client, _owner_user, member_user = site
    await _as(client, member_user["id"])
    response = await client.post(
        "/root/analytics/settings", data={"enabled": "0"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/chat"


# ── 4. БЕЗВРЕДНОСТЬ ──────────────────────────────────────────────────────────


async def test_capture_failure_never_breaks_the_request(site, monkeypatch) -> None:
    """Внутри писателя рвётся исключение — страница обязана отдаться как обычно."""
    client, owner_user, _member = site
    await _as(client, owner_user["id"])

    def _boom(**_kwargs):
        raise RuntimeError("аналитика сломалась")

    monkeypatch.setattr(analytics_mw.capture, "record", _boom)
    response = await client.get("/chat", follow_redirects=False)
    assert response.status_code == 200, "счётчик уронил страницу, которую считал"


async def test_flush_failure_loses_the_batch_but_not_the_process(db, monkeypatch) -> None:
    """Отказ записи в БД теряет пачку и НЕ копит её в памяти бесконечно."""
    capture._state["enabled"] = True
    capture._state["salt"] = "x" * 32
    capture._state["checked_at"] = 9e9

    async def _boom(_events):
        raise RuntimeError("диск кончился")

    monkeypatch.setattr(store, "insert_events", _boom)
    assert capture.record(kind=capture.KIND_VIEW, path="/chat", role="owner") is True
    assert capture.buffered() == 1
    assert await capture.flush() == 0
    assert capture.buffered() == 0, "пачка вернулась в буфер — это утечка памяти"


async def test_disabled_switch_stops_capture(site) -> None:
    """``analytics_enabled=0`` — сбор прекращается, страницы работают."""
    client, owner_user, _member = site
    async with get_connection() as conn:
        await set_kv(conn, capture.KV_ENABLED, "0")
        await conn.commit()
    capture.reset_cache()

    await _as(client, owner_user["id"])
    assert (await client.get("/chat", follow_redirects=False)).status_code == 200
    await capture.flush()

    assert await _rows("SELECT id FROM analytics_event") == []
    assert capture.is_enabled() is False


async def test_track_endpoint_ignores_anonymous_without_consent(site) -> None:
    """Клики анонима без «Принять» не пишутся: это уже поведение, а не счётчик."""
    client, _owner_user, _member = site
    client.cookies.clear()
    payload = {"events": [{"kind": "click", "label": "cta:signup", "path": "/landing"}]}

    anon = await client.post("/api/track", json=payload)
    assert anon.status_code == 200
    assert anon.json()["accepted"] == 0

    client.cookies.set(capture.CONSENT_COOKIE, capture.CONSENT_GRANTED)
    consented = await client.post("/api/track", json=payload)
    assert consented.json()["accepted"] == 1
    await capture.flush()

    rows = await _rows("SELECT kind, label, path FROM analytics_event WHERE kind='click'")
    assert rows == [("click", "cta:signup", "/landing")]


async def test_track_endpoint_never_trusts_the_client_path(site) -> None:
    """Произвольную строку в колонку путей вписать нельзя — только шаблон роута."""
    client, owner_user, _member = site
    await _as(client, owner_user["id"])
    await client.post(
        "/api/track",
        json={
            "events": [
                {"kind": "click", "label": "x", "path": "/../../etc/passwd?a=1"}
            ]
        },
    )
    await capture.flush()
    paths = [r[0] for r in await _rows("SELECT path FROM analytics_event WHERE kind='click'")]
    assert paths == ["(unmatched)"]


def _ev(day: str, path: str, role: str, session: str) -> dict:
    """Готовая строка события для фикстур — время внутри суток роли не играет."""
    return {
        "occurred_at": f"{day}T12:00:00",
        "day": day,
        "kind": capture.KIND_VIEW,
        "path": path,
        "label": "",
        "role": role,
        "device": "desktop",
        "referrer_host": "",
        "session_hash": session,
        "user_id": None,
        "first_view": 0,
        "status": 200,
    }
