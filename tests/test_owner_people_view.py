"""``/root/people`` — люди и рост. Сторожим границу, а не вёрстку.

Страница показывает владельцу адреса живых людей и даёт снести аккаунт одним
нажатием. Поэтому проверяется ровно то, что нельзя сломать молча:

1. **Граница приватности.** Владелец видит адреса и счётчики; чужой текст не
   появляется в разметке НИКОГДА — даже когда в чатах участника лежит
   канарейка (техника из ``test_member_data_isolation_audit.py``). Отдельно
   проверяется, что граница держится СТРУКТУРОЙ, а не аккуратностью: у
   ``Person`` нет ни одного текстового поля, а ``app/analytics/people.py`` не
   упоминает таблицы с чужим текстом даже по имени.
2. **Изоляция.** Участник не открывает страницу вовсе, и в отказе нет ни
   одного чужого адреса.
3. **Правильность цифр.** Регистрации по суткам сверяются с руками
   посчитанной фикстурой, включая границу суток; проценты — с нулевой базой,
   где деления не существует.
4. **Действия.** Заморозка реально закрывает вход, разморозка его возвращает,
   а удаление идёт КАСКАДОМ и уносит строки, которые голый
   ``DELETE FROM users`` оставил бы на диске.
"""

from __future__ import annotations

import io
import tokenize
from datetime import date
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.analytics import people
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection
from app.storage.repository import set_kv, set_user_kv
from app.web.main import create_app
from app.web.middleware import auth_gate
from app.web.routes import setup_gate

OWNER_PW = "Zq7-frost-lantern-91"
MEMBER_PW = "Kp4-velvet-harbour-38"
SECOND_PW = "Wt2-amber-thicket-63"

OWNER_EMAIL = "owner@people.test"
MEMBER_EMAIL = "member@people.test"
SECOND_EMAIL = "vtoroy@people.test"

#: Канарейка в чате участника. Строка уникальна и не встречается ни в шаблонах,
#: ни в переводах, поэтому любое совпадение в теле ответа — настоящая утечка.
C_MEMBER_CHAT = "KANAREYKA-CHAT-PEOPLE-PP01"
C_MEMBER_TITLE = "KANAREYKA-TITLE-PEOPLE-PP02"
C_MEMBER_MEMORY = "KANAREYKA-MEMORY-PEOPLE-PP03"


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


@pytest_asyncio.fixture
async def site(db: aiosqlite.Connection):
    """Владелец + два участника, у одного — чат с канарейкой и своя модель."""
    owner_user = await create_user(OWNER_EMAIL, OWNER_PW)
    member_user = await create_user(MEMBER_EMAIL, MEMBER_PW)
    second_user = await create_user(SECOND_EMAIL, SECOND_PW)

    await set_kv(db, "setup_complete", "true")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    await db.commit()
    setup_gate._cache.mark_done()
    _reset_auth_caches()

    # Чат участника с канарейкой И в заголовке, и в теле сообщения: заголовок
    # чата генерируется из первой реплики, поэтому он такой же чужой текст.
    async with get_connection() as conn:
        cur = await conn.execute(
            "INSERT INTO chat_session (user_id, title) VALUES (?, ?)",
            (member_user["id"], C_MEMBER_TITLE),
        )
        sid = cur.lastrowid
        await conn.execute(
            "INSERT INTO chat_message (session_id, role, content) VALUES (?, 'user', ?)",
            (sid, C_MEMBER_CHAT),
        )
        await conn.execute(
            "INSERT INTO user_memory (user_id, text) VALUES (?, ?)",
            (member_user["id"], C_MEMBER_MEMORY),
        )
        await conn.commit()
    # Свой провайдер участника — чтобы «подключил модель» было не нулём.
    await set_user_kv(db, member_user["id"], "llm_provider", "openai")
    await db.commit()

    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, owner_user, member_user, second_user
    finally:
        _reset_auth_caches()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


async def _seed_signups(pairs: list[tuple[str, str]]) -> None:
    """Проставить ``created_at`` конкретным аккаунтам (email → отметка времени)."""
    async with get_connection() as conn:
        for email, stamp in pairs:
            await conn.execute(
                "UPDATE users SET created_at = ? WHERE email = ?", (stamp, email)
            )
        await conn.commit()


# ── 1. ГРАНИЦА ПРИВАТНОСТИ ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_sees_every_account_with_email_and_counts(site) -> None:
    """Владелец получает страницу, а на ней — адреса, даты и счётчики."""
    client, owner_user, member_user, second_user = site
    await _as(client, owner_user["id"])

    response = await client.get("/root/people", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    for email in (OWNER_EMAIL, MEMBER_EMAIL, SECOND_EMAIL):
        assert email in body, f"адрес {email} не показан владельцу"
    assert "Всего аккаунтов" in body
    # Три аккаунта, один из которых подключил модель.
    view = await people.build_people_view(owner_id=owner_user["id"])
    assert view["counts"]["total"] == 3
    assert view["counts"]["with_llm"] == 1
    by_id = {p.id: p for p in view["people"]}
    assert by_id[member_user["id"]].chat_sessions == 1
    assert by_id[second_user["id"]].chat_sessions == 0
    assert by_id[member_user["id"]].llm_configured is True
    assert by_id[owner_user["id"]].is_owner is True


@pytest.mark.asyncio
async def test_page_shows_no_member_text_even_with_canaries_in_their_chats(
    site,
) -> None:
    """У участника есть чат, заголовок и память с канарейками — их тут нет.

    Это главный тест файла. Владельцу можно видеть, что у человека ОДИН чат;
    нельзя — что в нём написано. Заголовок чата проверяется отдельно, потому
    что он выглядит как безобидные метаданные, а собирается из первой реплики.
    """
    client, owner_user, _member, _second = site
    await _as(client, owner_user["id"])

    body = (await client.get("/root/people", follow_redirects=False)).text
    for canary in (C_MEMBER_CHAT, C_MEMBER_TITLE, C_MEMBER_MEMORY):
        assert canary not in body, f"текст участника утёк на страницу: {canary}"
    # …и при этом счётчик чатов на странице ЕСТЬ — «утечка закрыта» не должна
    # достигаться тем, что страница вообще ничего не показывает.
    assert "чатов" in body


def test_person_carries_no_free_text_field() -> None:
    """У :class:`Person` нет ни одного поля со свободным текстом — по списку.

    Список полей приколочен нарочно: добавить сюда ``display_name``,
    ``last_message`` или «просто заголовок последнего чата» = изменить границу
    приватности страницы, и это должно требовать правки теста, а не одной
    строки в дата-классе.
    """
    allowed = {
        "id",
        "email",
        "role",
        "status",
        "created_at",
        "created_day",
        "last_login_at",
        "last_active",
        "chat_sessions",
        "llm_configured",
        "is_owner",
    }
    assert set(people.Person.__dataclass_fields__) == allowed


def _sql_literals(path: Path) -> list[str]:
    """Все SQL-литералы модуля.

    SQL здесь — это ASCII-строка со словом SELECT/FROM. Условие «только ASCII»
    отсекает докстринг и русские комментарии, которые слово SELECT упоминают,
    но запросом не являются.
    """
    src = path.read_text(encoding="utf-8")
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.STRING:
            continue
        text = tok.string
        if not text.isascii():
            continue
        upper = text.upper()
        if "SELECT" in upper or "FROM " in upper:
            out.append(text)
    return out


def test_people_module_never_queries_a_table_holding_member_text() -> None:
    """Ни один SQL-литерал модуля не трогает чужой текст.

    Две независимые проверки, потому что обойти можно каждую по отдельности:

    * по ИСХОДНИКУ (минус докстринг модуля, где имена звучат легально) —
      названия таблиц с чужим текстом не встречаются вообще, даже в
      комментарии-заготовке;
    * по SQL-ЛИТЕРАЛАМ — среди них нет ни этих таблиц, ни колонки ``title``.
      ``title`` проверяется отдельно от таблиц: ``chat_session`` модулю
      РАЗРЕШЁН, но только ради ``COUNT(*)`` и ``MAX(updated_at)``, а заголовок
      чата собирается из первой реплики человека и потому является его текстом.
      Слово «title» при этом законно встречается в ключах вьюмодели
      (``{"title": "Вернулись на этой неделе"}``), поэтому по всему исходнику
      его искать нельзя — только в запросах.
    """
    path = Path(__file__).resolve().parents[1] / "app" / "analytics" / "people.py"
    src = path.read_text(encoding="utf-8")
    module_doc = people.__doc__ or ""
    body = src.replace(module_doc, "")
    forbidden = ("chat_message", "dm_message", "user_memory", "training_dataset")
    hits = [name for name in forbidden if name in body]
    assert not hits, (
        "app/analytics/people.py обратился к таблице с чужим текстом: "
        f"{hits}. Счётчики по таким таблицам берутся готовыми из "
        "app/analytics/funnel.py — см. шапку модуля."
    )

    sql = " ".join(_sql_literals(path)).lower()
    assert sql, "SQL-литералов не нашлось — проверка молча перестала работать"
    for name in (*forbidden, "title"):
        assert name not in sql, f"SQL модуля читает чужой текст: {name}"


@pytest.mark.asyncio
async def test_member_is_denied_and_sees_no_other_email(site) -> None:
    """Участнику страница недоступна, и в ответе нет ни одного чужого адреса."""
    client, _owner, member_user, _second = site
    await _as(client, member_user["id"])

    response = await client.get("/root/people", follow_redirects=False)
    assert response.status_code in (302, 303, 307, 403, 404), (
        f"участник получил {response.status_code} на owner-странице"
    )
    body = response.text
    for email in (OWNER_EMAIL, SECOND_EMAIL):
        assert email not in body, f"адрес {email} утёк участнику"
    for canary in (C_MEMBER_CHAT, C_MEMBER_TITLE):
        assert canary not in body


@pytest.mark.asyncio
async def test_member_never_sees_a_link_to_the_people_page(site) -> None:
    """Ссылки в разметке участника тоже нет: ссылка в DOM — это ссылка."""
    client, _owner, member_user, _second = site
    await _as(client, member_user["id"])
    body = (await client.get("/chat", follow_redirects=False)).text
    assert "/root/people" not in body


# ── 2. ПРАВИЛЬНОСТЬ ЦИФР ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signups_per_day_match_a_hand_computed_fixture(site) -> None:
    """Раскладка по суткам сверяется с руками посчитанной фикстурой.

    Отдельно проверена ГРАНИЦА СУТОК: 23:59:59 и 00:00:00 обязаны попасть в
    разные дни. Нарезка идёт ``substr(created_at, 1, 10)`` по UTC — тем же
    способом, что и во всех остальных отчётах этой базы.
    """
    _client, owner_user, member_user, second_user = site
    await _seed_signups(
        [
            (OWNER_EMAIL, "2026-08-24 23:59:59"),
            (MEMBER_EMAIL, "2026-08-25 00:00:00"),
            (SECOND_EMAIL, "2026-08-25 12:30:00"),
        ]
    )
    from app.analytics import store

    by_day = await store.registrations_by_day("2026-08-20", "2026-08-26")
    assert by_day == {"2026-08-24": 1, "2026-08-25": 2}, by_day

    view = await people.growth(today="2026-08-26")
    chart = view["charts"]["30"]
    assert len(chart["days"]) == 30
    assert chart["days"][-1] == "2026-08-26"
    assert chart["total"] == 3
    assert chart["series"][chart["days"].index("2026-08-25")] == 2
    assert chart["series"][chart["days"].index("2026-08-24")] == 1
    assert chart["series"][chart["days"].index("2026-08-26")] == 0

    # Помесячно — тем же срезом, без расхождения на границе.
    months = {m["month"]: m["count"] for m in await people.signups_by_month(12)}
    assert months == {"2026-08": 3}
    # Все три аккаунта отражены в people-строках теми же датами.
    rows = {p.id: p.created_day for p in await people.list_people(owner_id=owner_user["id"])}
    assert rows[owner_user["id"]] == "2026-08-24"
    assert rows[member_user["id"]] == "2026-08-25"
    assert rows[second_user["id"]] == "2026-08-25"


def test_pct_change_never_divides_by_zero() -> None:
    """Нулевая база — не 0 % и не 100 %, а «процента не существует»."""
    assert people.pct_change(5, 0) is None
    assert people.pct_change(0, 0) is None
    assert people.pct_change(0, 4) == -100.0
    assert people.pct_change(6, 4) == 50.0
    assert people.pct_change(4, 4) == 0.0


@pytest.mark.asyncio
async def test_comparisons_survive_an_empty_previous_period(site) -> None:
    """Сегодня есть регистрации, вчера/неделю/месяц назад — ни одной."""
    _client, _owner, _member, _second = site
    await _seed_signups(
        [
            (OWNER_EMAIL, "2026-08-26 09:00:00"),
            (MEMBER_EMAIL, "2026-08-26 10:00:00"),
            (SECOND_EMAIL, "2026-08-26 11:00:00"),
        ]
    )
    compare = {r["key"]: r for r in (await people.growth(today="2026-08-26"))["compare"]}

    assert compare["day"]["current"] == 3
    assert compare["day"]["previous"] == 0
    assert compare["day"]["delta"] == 3
    # Главное: не ZeroDivisionError и не выдуманное число.
    assert compare["day"]["pct"] is None
    assert compare["week"]["pct"] is None
    assert compare["month"]["pct"] is None


def test_compare_windows_align_partial_periods_and_survive_month_ends() -> None:
    """Неполная неделя сравнивается с ТАКИМ ЖЕ куском прошлой, не с полной.

    2026-08-26 — среда. Текущее окно недели: пн 24 → ср 26 (три дня). Прошлое
    обязано быть пн 17 → ср 19, а не пн 17 → вс 23: иначе каждый понедельник
    отчёт рисовал бы обвал на ровном месте.
    """
    windows = {w["key"]: w for w in people.compare_windows(date(2026, 8, 26))}
    assert windows["day"]["current"] == (date(2026, 8, 26), date(2026, 8, 26))
    assert windows["day"]["previous"] == (date(2026, 8, 25), date(2026, 8, 25))
    assert windows["week"]["current"] == (date(2026, 8, 24), date(2026, 8, 26))
    assert windows["week"]["previous"] == (date(2026, 8, 17), date(2026, 8, 19))
    assert windows["month"]["current"] == (date(2026, 8, 1), date(2026, 8, 26))
    assert windows["month"]["previous"] == (date(2026, 7, 1), date(2026, 7, 26))

    # 31 марта сравнивается с концом февраля, а не с несуществующим 31 февраля.
    march = {w["key"]: w for w in people.compare_windows(date(2026, 3, 31))}
    assert march["month"]["previous"] == (date(2026, 2, 1), date(2026, 2, 28))
    # Понедельник: текущее окно недели — ровно один день, прошлое тоже один.
    monday = {w["key"]: w for w in people.compare_windows(date(2026, 8, 24))}
    assert monday["week"]["current"] == (date(2026, 8, 24), date(2026, 8, 24))
    assert monday["week"]["previous"] == (date(2026, 8, 17), date(2026, 8, 17))
    # Первое число: месяц против первого дня прошлого месяца.
    first = {w["key"]: w for w in people.compare_windows(date(2026, 8, 1))}
    assert first["month"]["current"] == (date(2026, 8, 1), date(2026, 8, 1))
    assert first["month"]["previous"] == (date(2026, 7, 1), date(2026, 7, 1))


@pytest.mark.asyncio
async def test_cohort_health_reuses_the_existing_funnel_stages(site) -> None:
    """Судьба прошлой когорты берётся ступенями воронки, а не считается заново."""
    _client, owner_user, _member, _second = site
    # Прошлая неделя относительно среды 2026-08-26 — это 17…23 августа.
    await _seed_signups(
        [
            (MEMBER_EMAIL, "2026-08-18 10:00:00"),
            (SECOND_EMAIL, "2026-08-19 10:00:00"),
            (OWNER_EMAIL, "2026-08-01 10:00:00"),
        ]
    )
    data = await people.cohort_health(today="2026-08-26", owner_id=owner_user["id"])
    assert data["week_from"] == "2026-08-17"
    assert data["week_to"] == "2026-08-23"
    assert data["cohort"] == 2

    rows = {r["title"]: r for r in data["rows"]}
    # Один из двоих подключил модель и написал сообщение (фикстура site).
    assert rows["Подключили модель"]["count"] == 1
    assert rows["Написали первое сообщение"]["count"] == 1
    assert rows["Подключили модель"]["pct"] == 50.0

    # Те же числа обязаны совпасть со ступенями самой воронки — иначе на двух
    # соседних страницах стояли бы два разных ответа на один вопрос.
    from app.analytics import funnel

    stages = {
        s["key"]: s["count"]
        for s in (
            await funnel.build_funnel(
                "2026-08-17", "2026-08-23", owner_id=owner_user["id"]
            )
        )["stages"]
    }
    assert stages["llm"] == rows["Подключили модель"]["count"]
    assert stages["first_message"] == rows["Написали первое сообщение"]["count"]


# ── 3. ДЕЙСТВИЯ ──────────────────────────────────────────────────────────────


async def _login(client: AsyncClient, email: str, password: str):
    client.cookies.clear()
    return await client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_suspend_blocks_login_and_reactivate_restores_it(site) -> None:
    """Заморозка — не косметика: вход закрывается и открывается обратно."""
    client, owner_user, member_user, _second = site

    assert (await _login(client, MEMBER_EMAIL, MEMBER_PW)).status_code in (302, 303)

    await _as(client, owner_user["id"])
    res = await client.post(
        f"/root/users/{member_user['id']}/suspend",
        data={"next": "/root/people"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/root/people"

    blocked = await _login(client, MEMBER_EMAIL, MEMBER_PW)
    assert blocked.status_code == 403, "замороженный аккаунт всё ещё пускают"

    await _as(client, owner_user["id"])
    res = await client.post(
        f"/root/users/{member_user['id']}/approve",
        data={"next": "/root/people"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    again = await _login(client, MEMBER_EMAIL, MEMBER_PW)
    assert again.status_code in (302, 303), "разморозка не вернула вход"


@pytest.mark.asyncio
async def test_delete_goes_through_the_cascade_and_leaves_no_rows(site) -> None:
    """Удаление уносит чат, сообщение и память, а не только строку users.

    Голый ``DELETE FROM users`` прошёл бы этот тест частично: chat_message
    уезжает каскадом. Поэтому проверяется ещё и журнал ``account_deletion_log``
    с пометкой ``owner`` — след, который оставляет ИМЕННО каскадный удалятель.
    """
    client, owner_user, member_user, _second = site
    uid = member_user["id"]
    await _as(client, owner_user["id"])

    res = await client.post(
        f"/root/users/{uid}/delete",
        data={"next": "/root/people"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/root/people"

    async with get_connection() as conn:
        for sql, params in (
            ("SELECT COUNT(*) FROM users WHERE id = ?", (uid,)),
            ("SELECT COUNT(*) FROM chat_session WHERE user_id = ?", (uid,)),
            ("SELECT COUNT(*) FROM user_memory WHERE user_id = ?", (uid,)),
            ("SELECT COUNT(*) FROM user_settings WHERE user_id = ?", (uid,)),
        ):
            cur = await conn.execute(sql, params)
            assert int((await cur.fetchone())[0]) == 0, sql
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chat_message WHERE content = ?", (C_MEMBER_CHAT,)
        )
        assert int((await cur.fetchone())[0]) == 0, "текст сообщения остался на диске"
        cur = await conn.execute(
            "SELECT initiated_by FROM account_deletion_log WHERE user_id = ?", (uid,)
        )
        row = await cur.fetchone()
    assert row is not None, "каскад не оставил записи в журнале удалений"
    assert str(row[0]) == "owner", "журнал не различает удаление владельцем"

    # Аудит владельца тоже обязан знать об этом.
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'root.user.delete'"
        )
        assert int((await cur.fetchone())[0]) >= 1


@pytest.mark.asyncio
async def test_owner_account_cannot_be_deleted_from_the_page(site) -> None:
    """Гард владельца работает и через пульт: обезглавить инстанс нельзя."""
    client, owner_user, _member, _second = site
    await _as(client, owner_user["id"])
    await client.post(
        f"/root/users/{owner_user['id']}/delete",
        data={"next": "/root/people"},
        follow_redirects=False,
    )
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM users WHERE id = ?", (owner_user["id"],)
        )
        assert int((await cur.fetchone())[0]) == 1, "владелец удалился из пульта"


@pytest.mark.asyncio
async def test_member_cannot_mutate_accounts(site) -> None:
    """Мутирующая ручка тоже owner-only, а не «страница owner-only»."""
    client, _owner, member_user, second_user = site
    await _as(client, member_user["id"])
    res = await client.post(
        f"/root/users/{second_user['id']}/suspend",
        data={"next": "/root/people"},
        follow_redirects=False,
    )
    assert res.status_code in (302, 303, 307, 403, 404)
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT status FROM users WHERE id = ?", (second_user["id"],)
        )
        row = await cur.fetchone()
    assert str(row[0]) == "active", "участник заморозил чужой аккаунт"


@pytest.mark.asyncio
async def test_next_field_cannot_redirect_anywhere_it_likes(site) -> None:
    """``next`` — закрытый список, а не «любой путь из формы»."""
    client, owner_user, member_user, _second = site
    await _as(client, owner_user["id"])
    res = await client.post(
        f"/root/users/{member_user['id']}/approve",
        data={"next": "https://evil.example/steal"},
        follow_redirects=False,
    )
    assert res.headers["location"] == "/root"
