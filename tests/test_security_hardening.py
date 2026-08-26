"""Регресс-сторож ночного hardening'а перед открытием публичной регистрации.

Покрывает шесть блоков:

1. **Заголовки безопасности** — CSP/nosniff/Referrer-Policy/XFO/Permissions-
   Policy/HSTS, включая исключение для намеренно iframe-able ``/screenshot/*/embed``
   и то, что CSP НЕ вешается на не-HTML (иначе ломается service worker).
2. **Сессии** — ротация токена на входе и смене пароля, серверный отзыв на
   выходе, «выйти везде», idle-таймаут.
3. **CSRF** — вывод токена из сессии, режимы off/report/enforce, приём токена
   из заголовка/формы/query, исключения для машинных префиксов.
4. **Троттлинг** — per-user лимиты, освобождение владельца, урезанный бюджет
   для неподтверждённого email (только там, где почта реально уходит),
   дружелюбный 429.
5. **Пароли и локаут** — стойкость пароля, экспоненциальный backoff по АККАУНТУ.
6. **Доверенные прокси** — конфигурируемость через env/kv и одноразовый
   warning на XFF от чужого пира.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from httpx import ASGITransport, AsyncClient
from starlette.middleware import Middleware

from app.auth import account_state, lockout, proxies, verification
from app.auth import owner as owner_mod
from app.auth.password_policy import check_password
from app.auth.sessions import (
    SESSION_COOKIE_NAME,
    issue_session,
    revoke_all_for_user,
    rotate_session,
    verify_session,
)
from app.auth.users import create_user, validate_password
from app.storage.repository import set_kv
from app.web.middleware import auth_gate, csrf as csrf_mod, security_headers, throttle
from app.web.middleware.auth_gate import AuthGateMiddleware
from app.web.middleware.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CsrfMiddleware,
    csrf_token_for_session,
)
from app.web.middleware.security_headers import SecurityHeadersMiddleware
from app.web.middleware.throttle import ThrottleMiddleware


def _reset_all_caches() -> None:
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._role_gate_cache["value"] = False
    auth_gate._role_gate_cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0
    security_headers.reset_cache()
    csrf_mod.reset_cache()
    throttle.reset_state()
    verification.reset_cache()
    proxies.reset_cache()
    lockout.reset_all()
    account_state.reset_probe()
    # Сбрасываем счётчики скользящего окна между тестами.
    from app.web import rate_limit

    rate_limit._EVENTS.clear()


@pytest.fixture(autouse=True)
def _clean_caches():
    _reset_all_caches()
    yield
    _reset_all_caches()


# ── 1. Заголовки безопасности ────────────────────────────────────────────────


def _headers_app() -> FastAPI:
    app = FastAPI(middleware=[Middleware(SecurityHeadersMiddleware)])

    @app.get("/page", response_class=HTMLResponse)
    async def _page() -> HTMLResponse:
        return HTMLResponse("<!doctype html><p>hi</p>")

    @app.get("/voice", response_class=HTMLResponse)
    async def _voice() -> HTMLResponse:
        return HTMLResponse("<!doctype html><p>mic</p>")

    @app.get("/chat", response_class=HTMLResponse)
    async def _chat() -> HTMLResponse:
        return HTMLResponse("<!doctype html><p>mic</p>")

    @app.get("/screenshot/7/embed", response_class=HTMLResponse)
    async def _embed() -> HTMLResponse:
        response = HTMLResponse("<!doctype html><p>embed</p>")
        response.headers["X-Frame-Options"] = "ALLOWALL"
        return response

    @app.get("/api/thing.json")
    async def _json() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/static/sw.js")
    async def _sw() -> PlainTextResponse:
        return PlainTextResponse("//sw", media_type="application/javascript")

    return app


@pytest_asyncio.fixture
async def headers_client(db: aiosqlite.Connection):
    transport = ASGITransport(app=_headers_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_html_page_gets_the_full_header_set(headers_client) -> None:
    r = await headers_client.get("/page")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "Permissions-Policy" in r.headers


@pytest.mark.asyncio
async def test_csp_allows_every_resource_class_the_app_actually_loads(
    headers_client,
) -> None:
    """CSP выведена из инвентаря, а не угадана: проверяем каждый источник."""
    csp = (await headers_client.get("/page")).headers["Content-Security-Policy"]
    # self-hosted вендор-бандлы + инлайн-скрипты + Alpine (не CSP-сборка) + htmx
    assert "script-src 'self' 'unsafe-inline' 'unsafe-eval'" in csp
    # Яндекс.Метрика: скрипт, пиксель, вебвизор (XHR + WebSocket)
    assert "https://mc.yandex.ru" in csp
    assert "wss://mc.yandex.ru" in csp
    # Шрифты — свои, с /static/fonts/. Google-хостов в политике быть не должно:
    # <link> на fonts.googleapis.com срабатывал до баннера согласия, поэтому его
    # убрали вместе с разрешением в CSP (см. tests/test_no_third_party_fonts.py).
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp
    assert "font-src 'self' data:" in csp
    # 448 inline style= + Tailwind Play, который инжектит <style> в рантайме
    assert "style-src 'self' 'unsafe-inline'" in csp
    # data:/blob: картинки (SVG-фоны в CSS, createObjectURL-превью)
    assert "img-src" in csp and "data:" in csp and "blob:" in csp
    # локальный зонд загрузки ПК из chat_index.html — ДРУГОЙ origin
    assert "http://127.0.0.1:8770" in csp
    # запись голоса → Blob
    assert "media-src 'self' blob:" in csp
    # navigator.serviceWorker.register('/static/sw.js')
    assert "worker-src 'self'" in csp
    # WebAssembly в проекте нет — 'wasm-unsafe-eval' не выдаём
    assert "wasm-unsafe-eval" not in csp


@pytest.mark.asyncio
async def test_report_only_policy_is_stricter_and_blocks_nothing(headers_client) -> None:
    r = await headers_client.get("/page")
    report = r.headers["Content-Security-Policy-Report-Only"]
    assert "'unsafe-inline'" not in report
    assert "'unsafe-eval'" not in report
    # Он именно Report-Only: enforce-заголовок остаётся мягким.
    assert "'unsafe-inline'" in r.headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_report_only_can_be_switched_off_via_kv(
    headers_client, db: aiosqlite.Connection
) -> None:
    await set_kv(db, "csp_report_only", "0")
    security_headers.reset_cache()
    r = await headers_client.get("/page")
    assert "Content-Security-Policy-Report-Only" not in r.headers
    assert "Content-Security-Policy" in r.headers  # enforce остаётся


@pytest.mark.asyncio
async def test_embed_route_stays_framable(headers_client) -> None:
    """/screenshot/{id}/embed намеренно публичный — frame-ancestors не душит его."""
    r = await headers_client.get("/screenshot/7/embed")
    assert r.headers["X-Frame-Options"] == "ALLOWALL"
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors *" in csp
    assert "frame-ancestors 'self'" not in csp
    # Report-Only с frame-ancestors 'self' тут тоже не должен появиться.
    assert "Content-Security-Policy-Report-Only" not in r.headers


@pytest.mark.asyncio
async def test_non_html_gets_nosniff_but_no_csp(headers_client) -> None:
    """CSP на sw.js управляла бы fetch'ами самого service worker'а — не вешаем."""
    for path in ("/api/thing.json", "/static/sw.js"):
        r = await headers_client.get(path)
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" not in r.headers


@pytest.mark.asyncio
async def test_permissions_policy_denies_camera_and_geo_everywhere(
    headers_client,
) -> None:
    r = await headers_client.get("/page")
    policy = r.headers["Permissions-Policy"]
    assert "camera=()" in policy
    assert "geolocation=()" in policy
    assert "microphone=()" in policy  # обычная страница — микрофон запрещён


@pytest.mark.asyncio
async def test_permissions_policy_allows_microphone_only_on_voice_pages(
    headers_client,
) -> None:
    for path in ("/voice", "/chat"):
        policy = (await headers_client.get(path)).headers["Permissions-Policy"]
        assert "microphone=(self)" in policy
        assert "camera=()" in policy


@pytest.mark.asyncio
async def test_hsts_only_over_https(headers_client) -> None:
    plain = await headers_client.get("/page")
    assert "Strict-Transport-Security" not in plain.headers

    behind_proxy = await headers_client.get(
        "/page", headers={"X-Forwarded-Proto": "https"}
    )
    assert "max-age=31536000" in behind_proxy.headers["Strict-Transport-Security"]
    assert "includeSubDomains" in behind_proxy.headers["Strict-Transport-Security"]


# ── 2. Сессии ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_session_kills_the_old_token(db: aiosqlite.Connection) -> None:
    user = await create_user("rot@example.test", "correct-horse-battery")
    old, _ = await issue_session(user["id"])
    assert await verify_session(old) is not None

    new, _ = await rotate_session(old, user["id"])
    assert new != old
    assert await verify_session(old) is None, "старый токен обязан умереть"
    assert await verify_session(new) is not None


@pytest.mark.asyncio
async def test_revoke_all_for_user_can_keep_the_current_session(
    db: aiosqlite.Connection,
) -> None:
    user = await create_user("all@example.test", "correct-horse-battery")
    a, _ = await issue_session(user["id"])
    b, _ = await issue_session(user["id"])
    c, _ = await issue_session(user["id"])

    revoked = await revoke_all_for_user(user["id"], keep_token=c)
    assert revoked == 2
    assert await verify_session(a) is None
    assert await verify_session(b) is None
    assert await verify_session(c) is not None

    assert await revoke_all_for_user(user["id"]) == 1
    assert await verify_session(c) is None


@pytest.mark.asyncio
async def test_idle_session_is_refused_and_revoked(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_SESSION_IDLE_DAYS", "7")
    user = await create_user("idle@example.test", "correct-horse-battery")
    token, _ = await issue_session(user["id"])

    stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    await db.execute(
        "UPDATE auth_session SET last_seen_at = ? WHERE token = ?", (stale, token)
    )
    await db.commit()

    assert await verify_session(token) is None
    cursor = await db.execute(
        "SELECT revoked_at FROM auth_session WHERE token = ?", (token,)
    )
    row = await cursor.fetchone()
    assert row is not None and row["revoked_at"] is not None, (
        "протухшая по бездействию сессия должна быть погашена в БД, "
        "а не просто отвергнута в этом запросе"
    )


@pytest.mark.asyncio
async def test_idle_check_can_be_disabled(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_SESSION_IDLE_DAYS", "0")
    user = await create_user("noidle@example.test", "correct-horse-battery")
    token, _ = await issue_session(user["id"])
    stale = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    await db.execute(
        "UPDATE auth_session SET last_seen_at = ? WHERE token = ?", (stale, token)
    )
    await db.commit()
    assert await verify_session(token) is not None


# ── 3. CSRF ──────────────────────────────────────────────────────────────────


def _csrf_app() -> FastAPI:
    app = FastAPI(middleware=[Middleware(CsrfMiddleware)])

    @app.post("/do")
    async def _do(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"ok": True, "body_len": len(body)})

    @app.post("/api/do")
    async def _api_do() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post("/api/agent/upload")
    async def _agent() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/page", response_class=HTMLResponse)
    async def _page() -> HTMLResponse:
        return HTMLResponse("<!doctype html><p>hi</p>")

    return app


@pytest_asyncio.fixture
async def csrf_client(db: aiosqlite.Connection):
    transport = ASGITransport(app=_csrf_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def test_csrf_token_is_derived_from_the_session_and_unguessable() -> None:
    a = csrf_token_for_session("aaaa")
    b = csrf_token_for_session("bbbb")
    assert a and b and a != b
    assert csrf_token_for_session(a) != a  # не тождественная функция
    assert csrf_token_for_session(None) == ""
    assert csrf_token_for_session("aaaa") == a  # детерминирована


@pytest.mark.asyncio
async def test_csrf_cookie_is_published_for_a_signed_in_visitor(csrf_client) -> None:
    csrf_client.cookies.set(SESSION_COOKIE_NAME, "session-abc")
    r = await csrf_client.get("/page")
    assert r.cookies.get(CSRF_COOKIE_NAME) == csrf_token_for_session("session-abc")


@pytest.mark.asyncio
async def test_csrf_cookie_is_not_attached_to_every_asset(csrf_client) -> None:
    """Set-Cookie только на HTML: иначе он висит на каждом /static/* запросе."""
    csrf_client.cookies.set(SESSION_COOKIE_NAME, "session-abc")
    json_response = await csrf_client.post("/api/do", json={})
    assert "set-cookie" not in {k.lower() for k in json_response.headers}


@pytest.mark.asyncio
async def test_report_mode_logs_but_never_blocks(csrf_client) -> None:
    """Дефолт — report: 237 форм в чужих шаблонах ещё без токена, ломать нельзя."""
    csrf_client.cookies.set(SESSION_COOKIE_NAME, "session-abc")
    r = await csrf_client.post("/do", data={"x": "1"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_enforce_mode_rejects_a_missing_token(
    csrf_client, db: aiosqlite.Connection
) -> None:
    await set_kv(db, "csrf_mode", "enforce")
    csrf_mod.reset_cache()
    csrf_client.cookies.set(SESSION_COOKIE_NAME, "session-abc")
    r = await csrf_client.post("/do", data={"x": "1"})
    assert r.status_code == 403
    api = await csrf_client.post("/api/do", json={"x": 1})
    assert api.status_code == 403
    assert api.json()["detail"] == "csrf_failed"


@pytest.mark.asyncio
async def test_enforce_mode_accepts_header_form_field_and_query(
    csrf_client, db: aiosqlite.Connection
) -> None:
    await set_kv(db, "csrf_mode", "enforce")
    csrf_mod.reset_cache()
    csrf_client.cookies.set(SESSION_COOKIE_NAME, "session-abc")
    token = csrf_token_for_session("session-abc")

    by_header = await csrf_client.post(
        "/api/do", json={"x": 1}, headers={CSRF_HEADER_NAME: token}
    )
    assert by_header.status_code == 200

    by_field = await csrf_client.post("/do", data={"x": "1", "csrf_token": token})
    assert by_field.status_code == 200
    # Тело переиграно целиком — роут увидел форму, а не пустоту.
    assert by_field.json()["body_len"] > 0

    by_query = await csrf_client.post(f"/do?csrf_token={token}", data={"x": "1"})
    assert by_query.status_code == 200


@pytest.mark.asyncio
async def test_enforce_mode_rejects_a_wrong_token(
    csrf_client, db: aiosqlite.Connection
) -> None:
    await set_kv(db, "csrf_mode", "enforce")
    csrf_mod.reset_cache()
    csrf_client.cookies.set(SESSION_COOKIE_NAME, "session-abc")
    # Токен от ЧУЖОЙ сессии — ровно то, что подсунет same-site атакующий,
    # умеющий писать куки, но не читающий HttpOnly-сессию.
    r = await csrf_client.post(
        "/api/do",
        json={"x": 1},
        headers={CSRF_HEADER_NAME: csrf_token_for_session("someone-else")},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_machine_endpoints_and_anonymous_posts_are_exempt(
    csrf_client, db: aiosqlite.Connection
) -> None:
    await set_kv(db, "csrf_mode", "enforce")
    csrf_mod.reset_cache()
    # Без cookie сессии — нет ambient authority, нечего защищать (логин/регистрация).
    assert (await csrf_client.post("/do", data={"x": "1"})).status_code == 200
    # Машинный префикс с cookie — всё равно освобождён (агенты шлют свой токен).
    csrf_client.cookies.set(SESSION_COOKIE_NAME, "session-abc")
    assert (await csrf_client.post("/api/agent/upload")).status_code == 200
    # Bearer-аутентификация — не ambient, CSRF неприменим.
    assert (
        await csrf_client.post(
            "/api/do", json={}, headers={"Authorization": "Bearer x"}
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_safe_methods_are_never_checked(
    csrf_client, db: aiosqlite.Connection
) -> None:
    await set_kv(db, "csrf_mode", "enforce")
    csrf_mod.reset_cache()
    csrf_client.cookies.set(SESSION_COOKIE_NAME, "session-abc")
    assert (await csrf_client.get("/page")).status_code == 200


@pytest.mark.asyncio
async def test_csrf_mode_falls_back_to_report_not_off(
    csrf_client, db: aiosqlite.Connection
) -> None:
    """Мусор в kv не должен ВЫКЛЮЧАТЬ защиту — это fail-open."""
    await set_kv(db, "csrf_mode", "banana")
    csrf_mod.reset_cache()
    assert await csrf_mod._mode() == "report"


# ── 4. Троттлинг ─────────────────────────────────────────────────────────────


def _throttle_app() -> FastAPI:
    app = FastAPI(
        middleware=[Middleware(AuthGateMiddleware), Middleware(ThrottleMiddleware)]
    )

    @app.post("/api/copilot")
    async def _copilot() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post("/api/chat/sessions")
    async def _new_session() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post("/api/chat/sessions/{sid}/send")
    async def _send(sid: str) -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post("/api/chat/sessions/{sid}/rename")
    async def _rename(sid: str) -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/api/chat/sessions/{sid}/messages")
    async def _messages(sid: str) -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/chat", response_class=HTMLResponse)
    async def _chat() -> HTMLResponse:
        return HTMLResponse("<!doctype html><p>chat</p>")

    return app


@pytest_asyncio.fixture
async def throttle_setup(db: aiosqlite.Connection):
    owner_user = await create_user("owner@thr.test", "correct-horse-battery")
    member = await create_user("member@thr.test", "correct-horse-battery")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    _reset_all_caches()
    transport = ASGITransport(app=_throttle_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, owner_user, member


async def _sign_in(client: AsyncClient, user_id: int) -> None:
    token, _ = await issue_session(user_id)
    client.cookies.set(SESSION_COOKIE_NAME, token)


def _patch_delivery(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    """Force what ``app.smtp_delivery.delivery_status`` answers.

    ``"boom"`` makes the probe raise — the case where we cannot even decide
    whether mail works. Тесты не ходят в сеть и не читают .env разработчика
    (там реальный relay), поэтому подменяем именно источник правды.
    """

    async def _fake() -> str:
        if outcome == "boom":
            raise RuntimeError("kv read exploded")
        return outcome

    monkeypatch.setattr("app.smtp_delivery.delivery_status", _fake)
    verification.reset_cache()


@pytest.fixture
def mail_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Инстанс УМЕЕТ слать письма — только тогда штраф неподтверждённым честен."""
    _patch_delivery(monkeypatch, "ok")


@pytest.fixture
def no_smtp_env(monkeypatch: pytest.MonkeyPatch):
    """Убрать PERSONA_SMTP_* из окружения/.env, чтобы правило решал только kv."""
    from app.settings import get_settings

    for name in ("ENABLED", "HOST", "PORT", "USER", "PASS", "TO", "FROM", "TLS"):
        monkeypatch.setenv(f"PERSONA_SMTP_{name}", "")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    verification.reset_cache()
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_member_hits_a_429_with_a_friendly_russian_message(
    throttle_setup, db: aiosqlite.Connection, mail_works: None
) -> None:
    client, _owner, member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "4")  # /2 неподтверждённому = 2
    throttle.reset_cache()
    await _sign_in(client, member["id"])

    assert (await client.post("/api/copilot")).status_code == 200
    assert (await client.post("/api/copilot")).status_code == 200
    blocked = await client.post("/api/copilot")
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"
    assert "Слишком часто" in blocked.json()["error"]


@pytest.mark.asyncio
async def test_owner_is_never_throttled_out_of_his_own_instance(
    throttle_setup, db: aiosqlite.Connection
) -> None:
    client, owner_user, _member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "1")
    throttle.reset_cache()
    await _sign_in(client, owner_user["id"])
    for _ in range(6):
        assert (await client.post("/api/copilot")).status_code == 200


@pytest.mark.asyncio
async def test_verified_member_gets_the_full_budget(
    throttle_setup, db: aiosqlite.Connection, mail_works: None
) -> None:
    client, _owner, member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "8")
    throttle.reset_cache()
    await verification.mark_verified(member["id"])
    await _sign_in(client, member["id"])
    for _ in range(8):
        assert (await client.post("/api/copilot")).status_code == 200
    assert (await client.post("/api/copilot")).status_code == 429


@pytest.mark.asyncio
async def test_llm_budget_covers_generation_but_not_cheap_chat_crud(
    throttle_setup, db: aiosqlite.Connection, mail_works: None
) -> None:
    """Лимит модели не должен мешать ЧИТАТЬ и переименовывать свои чаты."""
    client, _owner, member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "2")  # /2 = 1 генерация
    throttle.reset_cache()
    await _sign_in(client, member["id"])

    assert (await client.post("/api/chat/sessions/7/send")).status_code == 200
    assert (await client.post("/api/chat/sessions/7/send")).status_code == 429

    # …а дешёвый CRUD того же семейства продолжает работать.
    for _ in range(6):
        assert (await client.post("/api/chat/sessions")).status_code == 200
        assert (await client.post("/api/chat/sessions/7/rename")).status_code == 200
        assert (
            await client.get("/api/chat/sessions/7/messages")
        ).status_code == 200


@pytest.mark.asyncio
async def test_page_navigations_are_not_throttled(
    throttle_setup, db: aiosqlite.Connection
) -> None:
    client, _owner, member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "1")
    throttle.reset_cache()
    await _sign_in(client, member["id"])
    for _ in range(5):
        assert (await client.get("/chat")).status_code == 200


@pytest.mark.asyncio
async def test_throttle_master_switch(throttle_setup, db: aiosqlite.Connection) -> None:
    client, _owner, member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "1")
    await set_kv(db, "throttle_enabled", "0")
    throttle.reset_cache()
    await _sign_in(client, member["id"])
    for _ in range(4):
        assert (await client.post("/api/copilot")).status_code == 200


@pytest.mark.asyncio
async def test_throttle_keys_on_the_user_not_the_ip(
    throttle_setup, db: aiosqlite.Connection, mail_works: None
) -> None:
    """Два аккаунта с одного IP не должны съедать бюджет друг друга."""
    client, owner_user, member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "2")  # /2 = 1 неподтверждённому
    throttle.reset_cache()
    second = await create_user("second@thr.test", "correct-horse-battery")

    await _sign_in(client, member["id"])
    assert (await client.post("/api/copilot")).status_code == 200
    assert (await client.post("/api/copilot")).status_code == 429

    await _sign_in(client, second["id"])
    assert (await client.post("/api/copilot")).status_code == 200


# ── 4b. Штраф за неподтверждённый email — только там, где почта уходит ───────
#
# Подтверждение ставится ТОЛЬКО переходом по ссылке из письма. Если инстанс не
# умеет слать письма (у прод-kv было ровно так: smtp_enabled='true', но пустые
# smtp_host/smtp_from), подтвердиться нельзя физически — значит и штрафовать
# не за что.


@pytest.mark.asyncio
async def test_smtp_enabled_with_an_empty_host_is_not_deliverable(
    db: aiosqlite.Connection, no_smtp_env: None
) -> None:
    """Ровно состояние прода: переключатель включён, релея нет."""
    from app.smtp_delivery import delivery_status

    await set_kv(db, "smtp_enabled", "true")
    await set_kv(db, "smtp_host", "")
    await set_kv(db, "smtp_from", "")
    verification.reset_cache()

    assert await delivery_status() == "misconfigured"
    assert await verification.mail_deliverable() is False
    assert await verification.unverified_penalty_applies(1) is False


@pytest.mark.asyncio
async def test_smtp_disabled_is_not_deliverable(
    db: aiosqlite.Connection, no_smtp_env: None
) -> None:
    from app.smtp_delivery import delivery_status

    await set_kv(db, "smtp_enabled", "false")
    await set_kv(db, "smtp_host", "smtp.example.test")
    await set_kv(db, "smtp_from", "persona@example.test")
    verification.reset_cache()

    assert await delivery_status() == "disabled"
    assert await verification.mail_deliverable() is False


@pytest.mark.asyncio
async def test_a_complete_smtp_config_is_deliverable(
    db: aiosqlite.Connection, no_smtp_env: None
) -> None:
    pytest.importorskip("aiosmtplib")
    from app.smtp_delivery import delivery_status

    await set_kv(db, "smtp_enabled", "true")
    await set_kv(db, "smtp_host", "smtp.example.test")
    await set_kv(db, "smtp_port", "587")
    await set_kv(db, "smtp_from", "persona@example.test")
    verification.reset_cache()

    assert await delivery_status() == "ok"
    assert await verification.mail_deliverable() is True
    # …и только здесь неподтверждённый аккаунт действительно платит.
    assert await verification.unverified_penalty_applies(1) is True


@pytest.mark.asyncio
async def test_a_failing_deliverability_probe_means_no_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка проверки — в пользу юзера: это анти-абуз, а не контроль доступа."""
    _patch_delivery(monkeypatch, "boom")
    assert await verification.mail_deliverable() is False
    assert await verification.unverified_penalty_applies(1) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["disabled", "misconfigured", "missing_dep", "boom"])
async def test_unverified_member_keeps_the_full_budget_without_working_mail(
    throttle_setup,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    client, _owner, member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "4")
    throttle.reset_cache()
    _patch_delivery(monkeypatch, outcome)
    await _sign_in(client, member["id"])

    for _ in range(4):  # весь бюджет, а не половина
        assert (await client.post("/api/copilot")).status_code == 200
    assert (await client.post("/api/copilot")).status_code == 429


@pytest.mark.asyncio
async def test_unverified_member_is_halved_when_mail_works(
    throttle_setup, db: aiosqlite.Connection, mail_works: None
) -> None:
    """Сегодняшнее поведение сохраняется там, где подтвердиться реально можно."""
    client, _owner, member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "4")
    throttle.reset_cache()
    await _sign_in(client, member["id"])

    for _ in range(2):
        assert (await client.post("/api/copilot")).status_code == 200
    assert (await client.post("/api/copilot")).status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "misconfigured", "boom"])
async def test_owner_is_exempt_whatever_the_mail_state(
    throttle_setup,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    client, owner_user, _member = throttle_setup
    await set_kv(db, "throttle_llm_per_5min", "1")
    throttle.reset_cache()
    _patch_delivery(monkeypatch, outcome)
    await _sign_in(client, owner_user["id"])

    for _ in range(6):
        assert (await client.post("/api/copilot")).status_code == 200


# ── 5. Пароли и локаут по аккаунту ───────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "short",              # < 8
        "password",           # блоклист
        "password1",          # блоклист + декорация
        "qwerty123",          # блоклист + декорация
        "12345678",           # прогон по ряду цифр
        "abcdefgh",           # прогон по алфавиту
        "aaaaaaaa",           # один символ
        "ghbdtn123",          # RU-раскладка «привет»
    ],
)
def test_weak_passwords_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        check_password(bad)


@pytest.mark.parametrize(
    "good",
    ["correct-horse-battery", "Zx9!plumRiver", "мойдлинныйпарольтут", "j7Qw2mNv"],
)
def test_reasonable_passwords_pass(good: str) -> None:
    check_password(good)


def test_password_may_not_simply_be_the_account_email() -> None:
    """Правило узкое НАМЕРЕННО — и это не расхождение теста с политикой.

    Первая версия проверки была «локальная часть email встречается где угодно
    в пароле». Она рубила ``owner-pass-123`` для ``owner@…`` и уронила 66
    существующих тестов — то есть на живых людях она рубила бы столько же
    нормальных парольных фраз. Отношение выгоды к трению отрицательное:
    угадать ``owner-pass-123``, зная адрес, ничуть не проще, чем любой другой
    пароль с дефисами.

    Поэтому политика ловит ровно то, что реально перебирают первым: пароль,
    который ЯВЛЯЕТСЯ логином (с точностью до цифрового/знакового хвоста) или
    состоит из него на ≥60%. Ассерты ниже — контракт, а не недосмотр.
    """
    for bad in ("ivanov12", "ivanov1234", "ivanov!!!"):
        with pytest.raises(ValueError):
            check_password(bad, email="ivanov@mail.test")
    check_password("ivanov-secret-99", email="ivanov@mail.test")
    check_password("owner-pass-123", email="owner@member.test")
    check_password("member-pass-123", email="member@member.test")


def test_absurdly_long_password_is_refused() -> None:
    """PBKDF2 стоит ~250 мс — 10 МБ «пароль» это CPU-DoS, а не безопасность."""
    with pytest.raises(ValueError):
        check_password("a1" * 600_000)


@pytest.mark.asyncio
async def test_create_user_enforces_the_new_floor(db: aiosqlite.Connection) -> None:
    with pytest.raises(ValueError):
        await create_user("weak@example.test", "password1")
    user = await create_user("strong@example.test", "correct-horse-battery")
    assert user["id"] > 0


def test_validate_password_keeps_the_legacy_eight_char_message() -> None:
    """Роут переводит по стабильному английскому ключу — он не должен уехать."""
    with pytest.raises(ValueError, match="at least 8 characters"):
        validate_password("short")


def test_account_lockout_uses_exponential_backoff() -> None:
    email = "target@example.test"
    for _ in range(lockout.FREE_ATTEMPTS):
        assert lockout.record_failure(email) == 0.0
        assert lockout.locked_for(email) == 0.0

    first = lockout.record_failure(email)
    assert first == pytest.approx(30.0)
    assert lockout.locked_for(email) > 0

    second = lockout.record_failure(email)
    assert second == pytest.approx(60.0)
    assert lockout.record_failure(email) == pytest.approx(120.0)


def test_lockout_is_capped() -> None:
    email = "cap@example.test"
    for _ in range(40):
        delay = lockout.record_failure(email)
    assert delay == pytest.approx(lockout.MAX_LOCK_SECONDS)


def test_successful_login_clears_the_lockout() -> None:
    email = "ok@example.test"
    for _ in range(lockout.FREE_ATTEMPTS + 2):
        lockout.record_failure(email)
    assert lockout.locked_for(email) > 0
    lockout.clear(email)
    assert lockout.locked_for(email) == 0.0


def test_lockout_does_not_enumerate_accounts() -> None:
    """Несуществующий адрес блокируется ровно так же, как существующий."""
    for _ in range(lockout.FREE_ATTEMPTS + 1):
        lockout.record_failure("nobody-here@example.test")
    assert lockout.locked_for("nobody-here@example.test") > 0


# ── 6. Доверенные прокси ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trusted_proxies_default_matches_the_previous_hardcoded_set(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(proxies.ENV_VAR, raising=False)
    proxies.reset_cache()
    assert await proxies.is_trusted_peer("127.0.0.1") is True
    assert await proxies.is_trusted_peer("192.168.33.3") is True
    assert await proxies.is_trusted_peer("8.8.8.8") is False


@pytest.mark.asyncio
async def test_trusted_proxies_env_override_supports_cidr(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(proxies.ENV_VAR, "10.0.0.0/8, 127.0.0.1")
    proxies.reset_cache()
    assert await proxies.is_trusted_peer("10.4.2.1") is True
    assert await proxies.is_trusted_peer("127.0.0.1") is True
    assert await proxies.is_trusted_peer("192.168.33.3") is False


@pytest.mark.asyncio
async def test_trusted_proxies_kv_override(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(proxies.ENV_VAR, raising=False)
    await set_kv(db, proxies.KV_KEY, "203.0.113.7")
    proxies.reset_cache()
    assert await proxies.is_trusted_peer("203.0.113.7") is True
    assert await proxies.is_trusted_peer("127.0.0.1") is False


@pytest.mark.asyncio
async def test_bad_proxy_entry_never_trusts_everyone(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(proxies.ENV_VAR, "not-an-ip, ???")
    proxies.reset_cache()
    for peer in ("127.0.0.1", "8.8.8.8", "unknown", ""):
        assert await proxies.is_trusted_peer(peer) is False


def test_untrusted_xff_warns_once_per_peer(caplog: pytest.LogCaptureFixture) -> None:
    proxies.reset_cache()
    proxies.note_untrusted_xff("8.8.8.8", "/auth/login")
    proxies.note_untrusted_xff("8.8.8.8", "/auth/login")
    proxies.note_untrusted_xff("8.8.8.8", "/auth/login")
    assert "8.8.8.8" in proxies._warned_peers
    assert len(proxies._warned_peers) == 1


def test_client_ip_ignores_xff_from_an_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web.routes.auth import _client_ip

    monkeypatch.delenv(proxies.ENV_VAR, raising=False)
    proxies.reset_cache()
    proxies._cache["value"] = proxies._parse("127.0.0.1")
    proxies._cache["checked_at"] = 1e18  # держим кэш «свежим»

    class _Client:
        host = "203.0.113.9"

    class _Req:
        client = _Client()
        headers = {"x-forwarded-for": "1.2.3.4"}
        url = type("U", (), {"path": "/auth/login"})()

    assert _client_ip(_Req()) == "203.0.113.9", "подделанный XFF не должен пройти"
    assert "203.0.113.9" in proxies._warned_peers


def test_client_ip_honours_xff_from_a_trusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web.routes.auth import _client_ip

    proxies.reset_cache()
    proxies._cache["value"] = proxies._parse("127.0.0.1")
    proxies._cache["checked_at"] = 1e18

    class _Client:
        host = "127.0.0.1"

    class _Req:
        client = _Client()
        headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1"}
        url = type("U", (), {"path": "/auth/login"})()

    assert _client_ip(_Req()) == "1.2.3.4"


# ── 6b. Блокировка аккаунта (users.status) действительно блокирует ───────────
#
# До этой ночи ``roles.set_status(uid, "suspended")`` только гасил сессии:
# ни ``authenticate``, ни ``verify_session`` в колонку ``status`` не смотрели,
# и человек заходил обратно тем же паролем. Это load-bearing для сегодняшнего
# открытия регистрации: в проде лежат спящие не-владельческие аккаунты (в т.ч.
# один, чей пароль нам неизвестен), и suspend — единственный рычаг владельца.


@pytest.mark.asyncio
async def test_suspended_account_cannot_log_in_with_the_right_password(
    db: aiosqlite.Connection,
) -> None:
    from app.auth.account_state import AccountInactiveError
    from app.auth.roles import set_status
    from app.auth.users import authenticate

    user = await create_user("susp@example.test", "correct-horse-battery")
    assert await authenticate("susp@example.test", "correct-horse-battery") is not None

    assert await set_status(user["id"], "suspended") is True
    with pytest.raises(AccountInactiveError) as excinfo:
        await authenticate("susp@example.test", "correct-horse-battery")
    assert excinfo.value.status == "suspended"


@pytest.mark.asyncio
async def test_suspension_does_not_leak_through_a_wrong_password(
    db: aiosqlite.Connection,
) -> None:
    """Неверный пароль даёт обычный None — статус аккаунта не перечисляется."""
    from app.auth.roles import set_status
    from app.auth.users import authenticate

    user = await create_user("quiet@example.test", "correct-horse-battery")
    await set_status(user["id"], "suspended")
    assert await authenticate("quiet@example.test", "wrong-password-here") is None


@pytest.mark.asyncio
async def test_pending_account_cannot_log_in(db: aiosqlite.Connection) -> None:
    from app.auth.account_state import AccountInactiveError
    from app.auth.roles import set_status
    from app.auth.users import authenticate

    user = await create_user("pend@example.test", "correct-horse-battery")
    await set_status(user["id"], "pending")
    with pytest.raises(AccountInactiveError):
        await authenticate("pend@example.test", "correct-horse-battery")


@pytest.mark.asyncio
async def test_reactivating_restores_access(db: aiosqlite.Connection) -> None:
    from app.auth.roles import set_status
    from app.auth.users import authenticate

    user = await create_user("back@example.test", "correct-horse-battery")
    await set_status(user["id"], "suspended")
    await set_status(user["id"], "active")
    assert await authenticate("back@example.test", "correct-horse-battery") is not None


@pytest.mark.asyncio
async def test_live_session_dies_when_the_account_is_suspended(
    db: aiosqlite.Connection,
) -> None:
    """Смена статуса действует даже без явного ревока сессий."""
    from app.auth.roles import set_status

    user = await create_user("live@example.test", "correct-horse-battery")
    token, _ = await issue_session(user["id"])
    assert await verify_session(token) is not None

    # Правим статус НАПРЯМУЮ, в обход set_status, — то есть без ревока сессий.
    await db.execute(
        "UPDATE users SET status = 'suspended' WHERE id = ?", (user["id"],)
    )
    await db.commit()

    assert await verify_session(token) is None
    cursor = await db.execute(
        "SELECT revoked_at FROM auth_session WHERE token = ?", (token,)
    )
    row = await cursor.fetchone()
    assert row is not None and row["revoked_at"] is not None

    # …а set_status по-прежнему гасит сессии сам.
    other = await create_user("live2@example.test", "correct-horse-battery")
    token2, _ = await issue_session(other["id"])
    await set_status(other["id"], "suspended")
    assert await verify_session(token2) is None


@pytest.mark.asyncio
async def test_magic_link_and_reset_refuse_a_suspended_account(
    db: aiosqlite.Connection,
) -> None:
    from app.auth.roles import set_status
    from app.auth.users import is_account_active

    user = await create_user("nomagic@example.test", "correct-horse-battery")
    assert await is_account_active(user["id"]) is True
    await set_status(user["id"], "suspended")
    assert await is_account_active(user["id"]) is False
    # Несуществующий id — тоже «не активен» (fail closed).
    assert await is_account_active(999_999) is False
    assert await is_account_active(None) is False


@pytest.mark.asyncio
async def test_owner_cannot_suspend_himself_out_of_the_instance(
    db: aiosqlite.Connection,
) -> None:
    """Гард roles.py на последнего owner должен остаться живым."""
    from app.auth.roles import set_status
    from app.auth.users import authenticate

    owner_user = await create_user("solo-owner@example.test", "correct-horse-battery")
    await db.execute("UPDATE users SET role = 'owner' WHERE id = ?", (owner_user["id"],))
    await db.commit()

    assert await set_status(owner_user["id"], "suspended") is False
    assert (
        await authenticate("solo-owner@example.test", "correct-horse-battery")
        is not None
    )


@pytest.mark.asyncio
async def test_login_route_refuses_a_suspended_account_with_a_clear_message(
    real_app_setup, db: aiosqlite.Connection
) -> None:
    from app.auth.roles import set_status

    client, _owner, member = real_app_setup
    await set_status(member["id"], "suspended")

    response = await client.post(
        "/auth/login",
        data={"email": "member@real.test", "password": "correct-horse-battery"},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 403
    assert "заблокирован" in response.json()["error"].lower()

    # А неверный пароль по тому же адресу — обычный 401, статус не выдаётся.
    wrong = await client.post(
        "/auth/login",
        data={"email": "member@real.test", "password": "definitely-not-it-42"},
        headers={"X-Requested-With": "fetch"},
    )
    assert wrong.status_code == 401
    assert "заблокирован" not in wrong.json()["error"].lower()


# ── 7. Сквозная проверка на НАСТОЯЩЕМ приложении ────────────────────────────
#
# Всё выше собирает мини-приложения из отдельных middleware. Здесь — реальный
# app.web.main.app со всем стеком: гейт, троттл, CSRF, заголовки, шаблоны.
# Смысл: убедиться, что порядок middleware в main.py действительно доносит
# заголовки до страниц владельца И участника, а не только в лаборатории.

_PUBLIC_PAGES = ("/landing", "/pricing")
_OWNER_PAGES = ("/", "/chat", "/voice")
_MEMBER_PAGES = ("/chat", "/voice", "/friends", "/messages")


@pytest_asyncio.fixture
async def real_app_setup(db: aiosqlite.Connection):
    from app.web.main import app as real_app

    owner_user = await create_user("owner@real.test", "correct-horse-battery")
    member = await create_user("member@real.test", "correct-horse-battery")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    # Гейт мастера установки читает ИМЕННО строку "true" (setup_gate.py).
    await set_kv(db, "setup_complete", "true")
    _reset_all_caches()

    transport = ASGITransport(app=real_app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        yield client, owner_user, member


def _assert_secure_html(response) -> None:
    assert response.status_code == 200, response.status_code
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    if "text/html" in response.headers.get("content-type", ""):
        assert "Content-Security-Policy" in response.headers
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "Permissions-Policy" in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PUBLIC_PAGES)
async def test_real_public_pages_carry_the_headers(real_app_setup, path: str) -> None:
    client, _owner, _member = real_app_setup
    _assert_secure_html(await client.get(path))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _OWNER_PAGES)
async def test_real_owner_pages_carry_the_headers(real_app_setup, path: str) -> None:
    client, owner_user, _member = real_app_setup
    await _sign_in(client, owner_user["id"])
    _assert_secure_html(await client.get(path))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _MEMBER_PAGES)
async def test_real_member_pages_carry_the_headers(real_app_setup, path: str) -> None:
    client, _owner, member = real_app_setup
    await _sign_in(client, member["id"])
    _assert_secure_html(await client.get(path))


@pytest.mark.asyncio
async def test_real_app_publishes_the_csrf_cookie_to_a_signed_in_member(
    real_app_setup,
) -> None:
    client, _owner, member = real_app_setup
    await _sign_in(client, member["id"])
    response = await client.get("/chat")
    assert response.status_code == 200
    session_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert client.cookies.get(CSRF_COOKIE_NAME) == csrf_token_for_session(
        session_token
    )


@pytest.mark.asyncio
async def test_get_logout_no_longer_logs_anyone_out(
    real_app_setup, db: aiosqlite.Connection
) -> None:
    """SameSite=Lax ШЛЁТ куку на top-level GET → GET-выход был CSRF на выход."""
    client, _owner, member = real_app_setup
    await _sign_in(client, member["id"])
    token = client.cookies.get(SESSION_COOKIE_NAME)

    page = await client.get("/auth/logout")
    assert page.status_code == 200
    assert "<form" in page.text and "post" in page.text.lower()
    assert await verify_session(token) is not None, (
        "GET /auth/logout обязан быть безопасным методом — он не должен "
        "гасить сессию"
    )

    done = await client.post("/auth/logout", follow_redirects=False)
    assert done.status_code == 303
    assert await verify_session(token) is None


@pytest.mark.asyncio
async def test_logout_everywhere_kills_every_device(
    real_app_setup, db: aiosqlite.Connection
) -> None:
    client, _owner, member = real_app_setup
    other_device, _ = await issue_session(member["id"])
    await _sign_in(client, member["id"])

    response = await client.post(
        "/auth/logout", data={"scope": "all"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert await verify_session(other_device) is None


@pytest.mark.asyncio
async def test_headers_land_on_redirects_and_auth_pages(real_app_setup) -> None:
    """Заголовки ставит САМАЯ внешняя middleware — значит и на 303, и на логин."""
    client, _owner, _member = real_app_setup
    login = await client.get("/auth/login")
    assert login.status_code == 200
    assert login.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "Content-Security-Policy" in login.headers

    bounced = await client.get("/timeline", follow_redirects=False)
    assert bounced.status_code in {301, 302, 303, 307}
    assert bounced.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_real_app_exposes_csrf_helpers_to_templates(real_app_setup) -> None:
    from app.web.templates_engine import templates

    assert "csrf_input" in templates.env.globals
    assert "csrf_token" in templates.env.globals


# ── 8. Личность на ПУБЛИЧНЫХ путях + область видимости в потоковом ответе ────
#
# Регресс, из-за которого owner-runbook на /help уезжал анонимам: гейт
# возвращал управление роутеру для публичного пути ДО того, как выставлял
# ``request.state.user_id``/``is_owner``. Шаблон, у которого нет атрибута,
# считал зрителя владельцем.


def _identity_app() -> FastAPI:
    """Публичный и приватный путь, каждый отдаёт то, что положил гейт."""
    app = FastAPI(middleware=[Middleware(AuthGateMiddleware)])

    async def _identity(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "user_id": getattr(request.state, "user_id", "ATTR-MISSING"),
                "is_owner": getattr(request.state, "is_owner", "ATTR-MISSING"),
            }
        )

    app.get("/help")(_identity)  # публичный префикс
    app.get("/landing")(_identity)  # публичный префикс
    app.get("/chat")(_identity)  # member-зона (приватная)
    return app


@pytest_asyncio.fixture
async def identity_setup(db: aiosqlite.Connection):
    owner_user = await create_user("owner@ident.test", "correct-horse-battery")
    member = await create_user("member@ident.test", "correct-horse-battery")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    _reset_all_caches()
    transport = ASGITransport(app=_identity_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, owner_user, member


@pytest.mark.asyncio
async def test_public_paths_still_resolve_who_is_looking(identity_setup) -> None:
    """/help публичен, но шаблон обязан узнать зрителя — иначе owner-утечка."""
    client, owner_user, member = identity_setup

    anon = (await client.get("/help")).json()
    assert anon["is_owner"] is False, "аноним НИКОГДА не owner"
    assert anon["user_id"] is None

    await _sign_in(client, member["id"])
    as_member = (await client.get("/help")).json()
    assert as_member["user_id"] == member["id"]
    assert as_member["is_owner"] is False

    await _sign_in(client, owner_user["id"])
    as_owner = (await client.get("/help")).json()
    assert as_owner["user_id"] == owner_user["id"]
    assert as_owner["is_owner"] is True, (
        "владелец на публичной странице должен опознаваться — иначе он видит "
        "member-версию своего же runbook'а"
    )


@pytest.mark.asyncio
async def test_public_path_access_decision_did_not_change(identity_setup) -> None:
    """Резолв личности не должен превратить публичный путь в приватный."""
    client, _owner, _member = identity_setup
    assert (await client.get("/landing")).status_code == 200
    assert (await client.get("/help")).status_code == 200
    # …а приватная зона по-прежнему закрыта для анонима.
    private = await client.get("/chat", follow_redirects=False)
    assert private.status_code in {301, 302, 303, 307}


@pytest.mark.asyncio
async def test_owner_help_page_shows_the_owner_shell(real_app_setup) -> None:
    client, owner_user, member = real_app_setup

    await _sign_in(client, owner_user["id"])
    owner_help = await client.get("/help")
    assert owner_help.status_code == 200
    assert 'href="/timeline"' in owner_help.text, (
        "владелец обязан видеть свой полный шелл на /help"
    )

    await _sign_in(client, member["id"])
    member_help = await client.get("/help")
    assert member_help.status_code == 200
    assert 'href="/timeline"' not in member_help.text

    client.cookies.delete(SESSION_COOKIE_NAME)
    anon_help = await client.get("/help")
    assert anon_help.status_code == 200
    assert 'href="/timeline"' not in anon_help.text, (
        "/help публичен — owner-навигация не должна уезжать в интернет"
    )


@pytest.mark.asyncio
async def test_scoped_body_holds_the_member_scope_and_leaves_nothing_behind() -> None:
    """Прямой тест механизма — с зубами.

    Интеграционный тест ниже сегодня зелёный ДАЖЕ БЕЗ обёртки: под
    ``BaseHTTPMiddleware`` роут и его генератор живут в дочерней задаче, чей
    контекст скопирован, пока личность ещё выставлена, а ``reset`` в родителе
    до копии не дотягивается (проверено отдельно). То есть сегодня гарантию
    даёт деталь реализации базового класса, а не наш код.

    Поэтому саму гарантию сторожим здесь, на уровне ``_scoped_body``: личность
    ОБЯЗАНА действовать внутри тела и ОБЯЗАНА исчезать после него — независимо
    от того, кто и как это тело крутит.
    """
    from app.request_ctx import current_member_uid
    from app.web.middleware.auth_gate import _scoped_body

    seen: list[int | None] = []

    async def _raw():
        seen.append(current_member_uid.get())
        yield b"chunk-1"
        seen.append(current_member_uid.get())
        yield b"chunk-2"

    assert current_member_uid.get() is None
    chunks = [c async for c in _scoped_body(_raw(), 77)]
    assert chunks == [b"chunk-1", b"chunk-2"]
    assert seen == [77, 77], "внутри тела должна действовать личность участника"
    assert current_member_uid.get() is None, "после тела личность не должна пережить"


@pytest.mark.asyncio
async def test_scoped_body_releases_the_scope_when_the_body_raises() -> None:
    from app.request_ctx import current_member_uid
    from app.web.middleware.auth_gate import _scoped_body

    async def _explodes():
        yield b"first"
        raise RuntimeError("stream died")

    with pytest.raises(RuntimeError):
        async for _ in _scoped_body(_explodes(), 77):
            pass
    assert current_member_uid.get() is None


@pytest.mark.asyncio
async def test_member_scope_survives_a_streaming_response_body(
    db: aiosqlite.Connection,
) -> None:
    """Контрактный тест сквозь весь стек: SSE-тело видит настройки УЧАСТНИКА.

    Тело выполняется ПОСЛЕ того, как ``dispatch`` вернул объект ответа, поэтому
    наивный сброс ContextVar в ``finally`` — известная ловушка. Здесь стрим
    зовёт настоящий ``get_theme()``, а не заглушку, и обязан прочитать тему
    участника, а не глобальную тему владельца.
    """
    from starlette.responses import StreamingResponse

    from app.request_ctx import current_member_uid
    from app.storage.db import get_connection
    from app.storage.repository import set_user_kv
    from app.web import templates_engine

    owner_user = await create_user("owner@stream.test", "correct-horse-battery")
    member = await create_user("member@stream.test", "correct-horse-battery")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    await set_kv(db, "theme", "cosmos")  # тема ВЛАДЕЛЬЦА (глобальный kv)
    async with get_connection() as conn:
        await set_user_kv(conn, member["id"], "theme", "light")  # тема участника
        await conn.commit()
    _reset_all_caches()
    # ``get_theme`` кэширует В ТРИ слоя, и все три процесс-глобальные: TTL-кэш
    # kv, TTL-кэш user_settings и ContextVar на запрос. Тест обязан обнулить
    # ВСЕ три, иначе он зелёный в одиночку и красный в полном прогоне (ровно
    # это и случилось: 1 failed из 1530 при первом полном прогоне).
    templates_engine._kv_value_cache.clear()
    templates_engine._user_kv_value_cache.clear()
    templates_engine.invalidate_theme_cache()

    app = FastAPI(middleware=[Middleware(AuthGateMiddleware)])

    @app.get("/chat/stream")
    async def _stream() -> StreamingResponse:
        async def _body():
            # Читаем ВНУТРИ тела — именно тут личность и должна действовать.
            # Отдаём и uid, чтобы падение сразу говорило ПОЧЕМУ: потеряна
            # личность (uid=None → глобальная тема владельца) или не доехала
            # строка user_settings (uid есть, а тема дефолтная).
            uid = current_member_uid.get()
            yield f"uid={uid} theme={templates_engine.get_theme()}".encode()

        return StreamingResponse(_body(), media_type="text/plain")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _sign_in(client, member["id"])
        response = await client.get("/chat/stream")
        assert response.status_code == 200
        assert response.text == f"uid={member['id']} theme=light", (
            "внутри потокового тела должна действовать личность УЧАСТНИКА, "
            f"а не глобальная тема владельца; получили {response.text!r} "
            f"(ожидали uid={member['id']}, theme=light; "
            "theme=cosmos → личность потеряна, theme=dark → не видно "
            "user_settings)"
        )


# ── 9. Страницы ошибок: 404/500 без утечки внутренностей ─────────────────────


@pytest.mark.asyncio
async def test_unknown_url_renders_a_page_not_raw_json(real_app_setup) -> None:
    client, owner_user, _member = real_app_setup
    await _sign_in(client, owner_user["id"])
    response = await client.get("/definitely-not-a-real-page-xyz")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert '{"detail":"Not Found"}' not in response.text
    assert "Такой страницы нет" in response.text
    # Заголовки безопасности доезжают и до страницы ошибки.
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_unknown_api_url_still_answers_json(real_app_setup) -> None:
    """Клиент, который ждёт JSON, не должен получить HTML-страницу."""
    client, owner_user, _member = real_app_setup
    await _sign_in(client, owner_user["id"])
    response = await client.get("/api/definitely-not-a-real-endpoint-xyz")
    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_500_page_never_leaks_exception_details(
    db: aiosqlite.Connection,
) -> None:
    from app.web.main import _install_error_handlers

    app = FastAPI(middleware=[Middleware(SecurityHeadersMiddleware)])

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("SECRET-/home/user/.persona/persona.db-DETAIL")

    @app.get("/api/boom")
    async def _api_boom() -> None:
        raise RuntimeError("SECRET-/home/user/.persona/persona.db-DETAIL")

    _install_error_handlers(app)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/boom")
        assert page.status_code == 500
        assert "SECRET" not in page.text
        assert "RuntimeError" not in page.text
        assert "Traceback" not in page.text
        assert "persona.db" not in page.text

        api = await client.get("/api/boom")
        assert api.status_code == 500
        assert "SECRET" not in api.text
        assert api.json() == {"detail": "internal server error"}


# ── Санитарная проверка: ничего не падает под нагрузкой конкурентных запросов ─


@pytest.mark.asyncio
async def test_headers_middleware_is_concurrency_safe(headers_client) -> None:
    results = await asyncio.gather(*(headers_client.get("/page") for _ in range(20)))
    assert all(r.headers["X-Content-Type-Options"] == "nosniff" for r in results)
