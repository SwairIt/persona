"""CSRF в режиме ``enforce``: настоящее приложение, настоящие потоки.

Отличие от ``test_security_hardening`` (там проверяется САМА middleware —
деривация токена, режимы, исключения) в том, что здесь поднимается полное
``create_app()`` и по нему ходит живой человек: владелец и участник. Каждый
поток проверяется ТРИЖДЫ:

* **с токеном** — должен пройти (не CSRF-403);
* **без токена** — 403 от middleware;
* **с чужим токеном** — 403 от middleware.

Откуда берётся токен в тесте
----------------------------
Тестовый клиент не исполняет JS, поэтому ``static/csrf.js`` здесь не работает.
Его поведение эмулирует :func:`_js_headers` — читает читаемую cookie
``persona_csrf`` и кладёт её в ``X-CSRF-Token`` ровно так же, как делает
скрипт для fetch/XHR/htmx. Это ЧЕСТНАЯ подмена только для тех потоков, где на
странице действительно есть JS-путь (fetch/htmx) или где csrf.js вставляет
скрытое поле в форму на submit.

Отдельно (``test_*_form_carries_token``) проверяются формы, где токен
отрендерил СЕРВЕР — их видно в HTML и они работают даже без JS. Это ровно те
шаблоны, которые не наследуют ``base.html`` (а значит не получают csrf.js) или
отправляются программным ``form.submit()`` (такой вызов не порождает событие
submit, и авто-инъекция скрипта мимо).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.web import templates_engine
from app.web.main import create_app
from app.web.middleware import auth_gate, csrf
from app.web.middleware.csrf import CSRF_COOKIE_NAME, CSRF_FIELD_NAME
from app.web.routes import setup_gate

CSRF_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "csrf.js"
BASE_HTML = (
    Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "base.html"
)

#: 64 hex-символа, но НЕ производные ни от одной живой сессии.
WRONG_TOKEN = "de" * 32

OWNER_PASSWORD = "Zq7-frost-lantern-91"
MEMBER_PASSWORD = "Kp4-velvet-harbour-38"
OWNER_EMAIL = "owner@csrf.test"
MEMBER_EMAIL = "member@csrf.test"


# ── инфраструктура ──────────────────────────────────────────────────────────


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
    templates_engine._kv_value_cache.clear()
    templates_engine._user_kv_value_cache.clear()
    templates_engine.invalidate_theme_cache()
    i18n.invalidate_language_cache()
    csrf.reset_cache()


@pytest_asyncio.fixture
async def env():
    """Настоящее приложение с ``csrf_mode=enforce``, владелец + участник."""
    await init_database()
    owner_user = await create_user(OWNER_EMAIL, OWNER_PASSWORD)
    member_user = await create_user(MEMBER_EMAIL, MEMBER_PASSWORD)
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner_user["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
        # Собственно предмет теста: защита ВКЛЮЧЕНА, а не «пишем в лог».
        await set_kv(conn, "csrf_mode", "enforce")
        await conn.commit()
    setup_gate._cache.mark_done()
    _reset_caches()

    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, owner_user, member_user
    finally:
        _reset_caches()


async def _login_as(client: AsyncClient, uid: int) -> None:
    """Выдать сессию и прогреть страницу, чтобы сервер поставил CSRF-cookie."""
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    # CSRF-cookie ставится только на HTML-ответ (см. _wrap_send): один заход на
    # страницу — ровно то, что делает браузер перед любым действием.
    await client.get("/chat")


def _js_headers(client: AsyncClient) -> dict[str, str]:
    """То, что делает ``static/csrf.js``: cookie → заголовок X-CSRF-Token."""
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value, "middleware не опубликовала cookie persona_csrf на HTML-ответе"
    return {"X-CSRF-Token": value}


def _is_csrf_reject(resp: Response) -> bool:
    """403 именно от CSRF-middleware, а не от гейта/роута.

    Различаем по телу: middleware отдаёт свой ``csrf_failed`` (JSON) или
    страницу «Запрос отклонён». Гейт на 403 пишет совсем другое, и путать эти
    два отказа нельзя — иначе тест «прошёл» бы на закрытом роуте.
    """
    if resp.status_code != 403:
        return False
    body = resp.text
    return "csrf_failed" in body or "Запрос отклонён" in body


def _hidden_token(html: str) -> str | None:
    """Токен из скрытого поля, которое отрендерил СЕРВЕР (без участия JS)."""
    match = re.search(
        rf'name="{CSRF_FIELD_NAME}"\s+value="([0-9a-f]{{64}})"', html
    )
    return match.group(1) if match else None


async def _probe(
    client: AsyncClient,
    method: str,
    url: str,
    *,
    label: str,
    json: Any = None,
    data: Any = None,
) -> Response:
    """Один поток × три попытки: с токеном / без / с чужим.

    Возвращает ответ УСПЕШНОЙ попытки, чтобы вызывающий мог продолжить сценарий
    (например забрать id созданной сессии чата).
    """
    kwargs: dict[str, Any] = {}
    if json is not None:
        kwargs["json"] = json
    if data is not None:
        kwargs["data"] = data

    ok = await client.request(method, url, headers=_js_headers(client), **kwargs)
    assert not _is_csrf_reject(ok), f"{label}: запрос С токеном отклонён CSRF-проверкой"

    missing = await client.request(method, url, **kwargs)
    assert _is_csrf_reject(missing), (
        f"{label}: запрос БЕЗ токена прошёл (status={missing.status_code}) — "
        "защита не работает"
    )

    wrong = await client.request(
        method, url, headers={"X-CSRF-Token": WRONG_TOKEN}, **kwargs
    )
    assert _is_csrf_reject(wrong), (
        f"{label}: запрос с ЧУЖИМ токеном прошёл (status={wrong.status_code})"
    )
    return ok


# ── 1. Доставка токена в браузер ────────────────────────────────────────────


def test_csrf_js_is_loaded_before_htmx_and_alpine() -> None:
    """Скрипт обязан стоять раньше htmx/Alpine и БЕЗ defer.

    Он патчит ``window.fetch`` — если хоть один сценарий успеет сделать POST до
    патча, тот уйдёт без заголовка и получит 403 у живого пользователя.
    """
    html = BASE_HTML.read_text(encoding="utf-8")
    pos_csrf = html.find("/static/csrf.js")
    pos_htmx = html.find("htmx-2.0.4.min.js")
    pos_alpine = html.find("alpine-3.14.7.min.js")
    assert pos_csrf > 0, "base.html не подключает /static/csrf.js"
    assert pos_csrf < pos_htmx < pos_alpine

    tag = html[html.rfind("<script", 0, pos_csrf) : html.find(">", pos_csrf) + 1]
    assert "defer" not in tag and "async" not in tag, (
        "csrf.js должен исполняться синхронно, до любого нашего кода"
    )


def test_csrf_js_patches_every_transport() -> None:
    """Проверяем ПОВЕДЕНИЕ отгруженного скрипта, а не веру в handover."""
    js = CSRF_JS.read_text(encoding="utf-8")
    assert "window.fetch = function" in js, "fetch не патчится"
    assert "XMLHttpRequest.prototype.send" in js, "XHR не патчится"
    assert 'addEventListener("htmx:configRequest"' in js, "htmx не покрыт"
    # Авто-инъекция скрытого поля в обычные формы.
    assert 'addEventListener(\n    "submit"' in js or '"submit"' in js
    assert 'input.name = "csrf_token"' in js, "поле в формы не вставляется"
    # multipart: тело аплоада middleware не разбирает, спасает только query.
    assert 'searchParams.set("csrf_token"' in js, (
        "multipart-формы остались без токена: middleware не читает их тело"
    )


@pytest.mark.asyncio
async def test_html_page_publishes_readable_cookie(env) -> None:
    client, _owner, member = env
    client.cookies.clear()
    token, _ = await issue_session(member["id"])
    client.cookies.set(SESSION_COOKIE_NAME, token)
    resp = await client.get("/chat")
    assert resp.status_code == 200
    assert client.cookies.get(CSRF_COOKIE_NAME), "cookie persona_csrf не выдана"
    assert "/static/csrf.js" in resp.text, "страница не тянет csrf.js"


# ── 2. Формы, где токен рендерит сервер (работают и без JS) ─────────────────


@pytest.mark.asyncio
async def test_base_logout_form_carries_server_rendered_token(env) -> None:
    """Выход из аккаунта не должен зависеть от того, доехал ли JS."""
    client, _owner, member = env
    await _login_as(client, member["id"])
    page = await client.get("/chat")
    token = _hidden_token(page.text)
    assert token, "в base.html форма выхода без скрытого csrf_token"

    # Настоящая форма: токен из HTML, тело — urlencoded, заголовка нет вовсе.
    denied = await client.post("/auth/logout", data={"scope": ""})
    assert _is_csrf_reject(denied)

    ok = await client.post(
        "/auth/logout", data={"scope": "", CSRF_FIELD_NAME: token}
    )
    assert not _is_csrf_reject(ok)
    assert ok.status_code == 303


@pytest.mark.asyncio
async def test_standalone_landing_logout_form_carries_token(env) -> None:
    """``landing_v2.html`` не наследует base.html → csrf.js там нет."""
    client, _owner, member = env
    await _login_as(client, member["id"])
    page = await client.get("/landing")
    assert page.status_code == 200
    assert "/auth/logout" in page.text
    assert _hidden_token(page.text), "форма выхода на лендинге без токена"


@pytest.mark.asyncio
async def test_standalone_billing_forms_carry_token(env) -> None:
    """``billing.html`` — тоже standalone: три формы, все с токеном."""
    client, owner_user, _member = env
    await _login_as(client, owner_user["id"])
    page = await client.get("/billing")
    if page.status_code != 200:
        pytest.skip(f"/billing недоступен в этой конфигурации: {page.status_code}")
    assert _hidden_token(page.text), "billing.html без серверного csrf_token"


@pytest.mark.asyncio
async def test_programmatic_submit_forms_carry_token(env) -> None:
    """``form.submit()`` не порождает submit-событие → csrf.js бессилен.

    Такие формы обязаны нести поле в разметке, иначе живой пользователь
    получит 403 там, где просто дёрнул селект.
    """
    client, owner_user, _member = env
    await _login_as(client, owner_user["id"])

    for path, why in (
        ("/settings/ai-everywhere", 'onchange="this.form.submit()"'),
        ("/settings/system-prompt", "promptResetForm.submit()"),
        ("/root", 'select role → this.form.submit()'),
    ):
        page = await client.get(path)
        assert page.status_code == 200, f"{path} → {page.status_code}"
        assert _hidden_token(page.text), f"{path}: нет серверного токена ({why})"


@pytest.mark.asyncio
async def test_multipart_forms_carry_query_token(env) -> None:
    """Аплоады: middleware намеренно не буферизует тело → нужен ?csrf_token=."""
    client, owner_user, _member = env
    await _login_as(client, owner_user["id"])
    page = await client.get("/settings/backup/manage")
    assert page.status_code == 200
    assert re.search(r'action="/settings/backup/import\?csrf_token=[0-9a-f]{64}"', page.text), (
        "multipart-форма импорта без ?csrf_token= — скрытое поле middleware не видит"
    )


@pytest.mark.asyncio
async def test_every_patched_template_renders_its_token(env) -> None:
    """Каждая правка шаблона реально доезжает до HTML (и ничего не роняет).

    Тут проверяется не поток, а факт: страница отдаёт 200 и на ней есть токен
    в том виде, в каком его ждёт middleware — скрытым полем либо ``?csrf_token=``
    для multipart.
    """
    client, owner_user, _member = env
    await _login_as(client, owner_user["id"])

    # multipart-аплоады: только query спасает. Страница обязана рендериться
    # (значит csrf_token(request) в ней не падает), а форма — нести токен.
    # Часть форм живёт внутри {% for %} и на пустой БД не рисуется, поэтому там,
    # где рендер не гарантирован, сторожим ИСТОЧНИК шаблона.
    for path, action in (
        ("/settings/integrations", "/settings/integrations/import-markdown"),
        ("/admin/notes-csv-import", "/admin/notes-csv-import"),
    ):
        page = await client.get(path)
        assert page.status_code == 200, f"{path} → {page.status_code}"
        assert re.search(rf'{re.escape(action)}\?csrf_token=[0-9a-f]{{64}}"', page.text), (
            f"{path}: multipart-форма без ?csrf_token="
        )

    icons = await client.get("/settings/app-icons")
    assert icons.status_code == 200
    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "web" / "templates" / "app_icons_admin.html"
    ).read_text(encoding="utf-8")
    uploads = re.findall(r'action="/app-icon/[^"]*?/upload([^"]*)"', src)
    assert uploads, "формы загрузки иконок пропали из шаблона"
    assert all("csrf_token=" in tail for tail in uploads), (
        "форма загрузки иконки без ?csrf_token= — multipart-тело middleware не читает"
    )

    # Форма, которую JS строит сам (`document.createElement('form')`).
    recycle = await client.get("/recycle")
    assert recycle.status_code == 200
    if "postWithConfirm" in recycle.text:
        assert re.search(r'var token = "[0-9a-f]{64}"', recycle.text), (
            "/recycle: форма собирается в JS, а токена на странице нет"
        )

    # Standalone-страница с fetch-POST: спасает только подключённый csrf.js.
    tour = await client.get("/tour")
    assert tour.status_code == 200
    assert "/static/csrf.js" in tour.text, "/tour без csrf.js — POST /api/tour/* даст 403"

    # Standalone-форма смены пароля.
    setpw = await client.get("/auth/set-password")
    assert setpw.status_code == 200
    assert _hidden_token(setpw.text), "/auth/set-password без серверного токена"


@pytest.mark.asyncio
async def test_query_token_is_accepted(env) -> None:
    """Лазейка ``?csrf_token=`` (на неё опираются multipart-формы) — жива."""
    client, _owner, member = env
    await _login_as(client, member["id"])
    token = client.cookies.get(CSRF_COOKIE_NAME)
    resp = await client.post(
        f"/api/chat/sessions?csrf_token={token}", json={"title": "через query"}
    )
    assert not _is_csrf_reject(resp)
    assert resp.status_code == 201


# ── 3. Потоки участника ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_is_exempt_and_still_works(env) -> None:
    """Вход — до сессии, ambient authority ещё нет → токена не требуем."""
    client, _owner, _member = env
    client.cookies.clear()
    resp = await client.post(
        "/auth/login", data={"email": MEMBER_EMAIL, "password": MEMBER_PASSWORD}
    )
    assert not _is_csrf_reject(resp), "логин потребовал CSRF-токен — форма сломана"
    assert resp.status_code in (200, 303)
    assert client.cookies.get(SESSION_COOKIE_NAME), "сессия не выдана"


@pytest.mark.asyncio
async def test_member_chat_flows(env) -> None:
    """Создать сессию → отправить сообщение → переименовать → режим."""
    client, _owner, member = env
    await _login_as(client, member["id"])

    created = await _probe(
        client, "POST", "/api/chat/sessions",
        label="создание сессии чата", json={"title": "CSRF"},
    )
    session_id = created.json()["id"]

    await _probe(
        client, "POST", f"/api/chat/sessions/{session_id}/send",
        label="отправка сообщения в чат", json={"content": "привет"},
    )
    await _probe(
        client, "POST", f"/api/chat/sessions/{session_id}/rename",
        label="переименование сессии", json={"title": "новое имя"},
    )
    await _probe(
        client, "POST", f"/api/chat/sessions/{session_id}/mode",
        label="смена режима сессии", json={"mode": "chat"},
    )


@pytest.mark.asyncio
async def test_member_settings_flows(env) -> None:
    """Тема, характер, расширенный режим, уведомления — формы кабинета."""
    client, _owner, member = env
    await _login_as(client, member["id"])

    await _probe(
        client, "POST", "/settings/theme",
        label="смена темы", data={"theme": "light"},
    )
    await _probe(
        client, "POST", "/settings/system-prompt",
        label="сохранение системного промпта",
        data={"prompt_text": "Ты — резкий, но честный друг."},
    )
    await _probe(
        client, "POST", "/settings/advanced",
        label="переключение расширенного флага",
        data={"master": "1", "recall_mode": "keyword"},
    )
    await _probe(
        client, "POST", "/settings/notifications-social",
        label="настройки уведомлений",
        data={"friend_request__web": "1"},
    )


@pytest.mark.asyncio
async def test_social_flows(env) -> None:
    """Заявка в друзья → приём → ЛС → ИИ-режим переписки."""
    client, owner_user, member = env

    # Заявку принимает только НАХОДИМЫЙ аккаунт — тумблер сам по себе
    # state-changing, поэтому проверяем его тем же тройным прогоном.
    await _login_as(client, owner_user["id"])
    await _probe(
        client, "POST", "/api/friends/discoverable",
        label="тумблер «меня можно найти»", json={"value": True},
    )

    await _login_as(client, member["id"])
    sent = await _probe(
        client, "POST", "/api/friends/request",
        label="заявка в друзья", json={"to_user_id": owner_user["id"]},
    )
    assert sent.status_code == 200, sent.text
    request_id = sent.json()["request_id"]

    await _login_as(client, owner_user["id"])
    await _probe(
        client, "POST", f"/api/friends/{request_id}/accept",
        label="приём заявки в друзья",
    )

    # Ветка заводится тем же переходом, каким её открывает человек со страницы
    # друзей: GET /messages/with/{id} → 303 на /messages/{thread_id}.
    await _login_as(client, member["id"])
    opened = await client.get(f"/messages/with/{owner_user['id']}")
    assert opened.status_code == 303, opened.text
    thread_id = int(opened.headers["location"].rsplit("/", 1)[-1])

    await _probe(
        client, "POST", f"/api/messages/{thread_id}/send",
        label="отправка ЛС", json={"body": "первое сообщение"},
    )
    await _probe(
        client, "POST", f"/api/messages/{thread_id}/ai",
        label="ИИ-режим переписки",
        json={"mode": "off", "style_note": "", "quota_daily": 5},
    )


@pytest.mark.asyncio
async def test_member_skills_install_and_remove(env, monkeypatch) -> None:
    """Установка/удаление навыка — обычные формы на member-поверхности."""
    from app.web.routes import skills_settings

    async def _fake_fetch(url: str):
        return "csrf-test-skill", "# skill\nтело навыка", url

    monkeypatch.setattr(skills_settings, "fetch_skill_from_github", _fake_fetch)

    client, _owner, member = env
    await _login_as(client, member["id"])

    await _probe(
        client, "POST", "/settings/skills/install",
        label="установка навыка",
        data={"url": "https://github.com/example/skill/blob/main/SKILL.md"},
    )

    listing = await client.get("/api/skills")
    assert listing.status_code == 200
    skills = listing.json().get("skills") or listing.json().get("items") or []
    assert skills, "навык не установился — проверять удаление нечего"
    skill_id = skills[0]["id"]

    await _probe(
        client, "POST", f"/settings/skills/{skill_id}/toggle",
        label="переключение навыка", data={"enabled": ""},
    )
    await _probe(
        client, "POST", f"/settings/skills/{skill_id}/delete",
        label="удаление навыка",
    )


@pytest.mark.asyncio
async def test_llm_grant_issue_and_revoke(env) -> None:
    """Выдача и отзыв доступа к модели — формы ``/settings/llm/sharing``."""
    client, owner_user, member = env

    # Сначала дружба: выдать доступ можно только другу.
    await _login_as(client, owner_user["id"])
    disc = await client.post(
        "/api/friends/discoverable",
        json={"value": True},
        headers=_js_headers(client),
    )
    assert disc.status_code in (200, 303)

    await _login_as(client, member["id"])
    sent = await client.post(
        "/api/friends/request",
        json={"to_user_id": owner_user["id"]},
        headers=_js_headers(client),
    )
    assert sent.status_code == 200, sent.text
    request_id = sent.json()["request_id"]

    await _login_as(client, owner_user["id"])
    accepted = await client.post(
        f"/api/friends/{request_id}/accept", headers=_js_headers(client)
    )
    assert accepted.status_code == 200

    # Владелец делится своей моделью с участником.
    await _probe(
        client, "POST", "/settings/llm/sharing/grant",
        label="выдача доступа к модели",
        data={"friend_id": str(member["id"]), "daily_limit": "10", "note": "csrf"},
    )

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM llm_grant WHERE grantor_id = ? ORDER BY id DESC LIMIT 1",
            (int(owner_user["id"]),),
        )
        row = await cursor.fetchone()
    grant_id = int(row[0]) if row else 999_999

    await _probe(
        client, "POST", f"/settings/llm/sharing/{grant_id}/revoke",
        label="отзыв доступа к модели",
    )


@pytest.mark.asyncio
async def test_owner_flows(env) -> None:
    """Владелец: смена роли (программный submit) и его собственные настройки."""
    client, owner_user, member = env
    await _login_as(client, owner_user["id"])

    await _probe(
        client, "POST", "/settings/theme",
        label="владелец: смена темы", data={"theme": "cosmos"},
    )
    await _probe(
        client, "POST", "/settings/system-prompt/reset",
        label="владелец: сброс промпта",
    )
    await _probe(
        client, "POST", "/settings/ai-everywhere",
        label="владелец: тумблер «ИИ везде»", data={"ai_everywhere": "1"},
    )
    await _probe(
        client, "POST", f"/root/users/{member['id']}/role",
        label="владелец: смена роли участника", data={"role": "member"},
    )


@pytest.mark.asyncio
async def test_logout_last(env) -> None:
    """Выход — тоже state-changing, и тоже под защитой."""
    client, _owner, member = env
    await _login_as(client, member["id"])
    await _probe(client, "POST", "/auth/logout", label="выход", data={"scope": ""})


# ── 4. Освобождённые поверхности ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_machine_prefixes_still_work_without_token(env) -> None:
    """Машинные вызовы аутентифицируются своим токеном — CSRF им не нужен."""
    client, _owner, member = env
    await _login_as(client, member["id"])  # даже С cookie-сессией

    for path, payload in (
        ("/api/sync/push", {"items": []}),
        ("/api/llm/worker/1/done", {"ok": True}),
        ("/billing/webhook", {"event": "noop"}),
    ):
        resp = await client.post(path, json=payload)
        assert not _is_csrf_reject(resp), f"{path} освобождён, но получил CSRF-403"


@pytest.mark.asyncio
async def test_bearer_auth_is_exempt(env) -> None:
    """``Authorization:`` — не ambient authority, CSRF-токен не требуется."""
    client, _owner, member = env
    await _login_as(client, member["id"])
    resp = await client.post(
        "/api/chat/sessions",
        json={"title": "bearer"},
        headers={"Authorization": "Bearer whatever"},
    )
    assert not _is_csrf_reject(resp)


@pytest.mark.asyncio
async def test_safe_methods_never_checked(env) -> None:
    client, _owner, member = env
    await _login_as(client, member["id"])
    for path in ("/chat", "/settings/theme", "/friends", "/messages"):
        resp = await client.get(path)
        assert not _is_csrf_reject(resp), f"GET {path} отклонён CSRF-проверкой"


@pytest.mark.asyncio
async def test_sessionless_post_is_exempt(env) -> None:
    """Нет cookie сессии — нечего подделывать; регистрация/логин не ломаем."""
    client, _owner, _member = env
    client.cookies.clear()
    resp = await client.post("/auth/login", data={"email": "nobody@x.test", "password": "x"})
    assert not _is_csrf_reject(resp)


# ── 5. Режимы ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_mode_lets_the_same_request_through(env) -> None:
    """Контроль: без токена падает ИМЕННО из-за enforce, а не из-за роута."""
    client, _owner, member = env
    await _login_as(client, member["id"])

    denied = await client.post("/api/chat/sessions", json={"title": "нет токена"})
    assert _is_csrf_reject(denied)

    async with get_connection() as conn:
        await set_kv(conn, "csrf_mode", "report")
        await conn.commit()
    csrf.reset_cache()

    allowed = await client.post("/api/chat/sessions", json={"title": "нет токена"})
    assert allowed.status_code == 201, "в report-режиме запрос обязан проходить"
