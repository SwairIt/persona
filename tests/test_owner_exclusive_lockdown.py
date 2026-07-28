"""Owner-exclusive lockdown: enrollment, auth, middleware and session revocation."""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport, AsyncClient

from app.auth import exclusive, lockdown, owner
from app.auth.sessions import (
    SESSION_COOKIE_NAME,
    count_active_non_owner_sessions,
    issue_session,
    revoke_non_owner_sessions,
)
from app.auth.users import create_user
from app.storage.repository import set_kv
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware
from app.web.routes import auth as auth_routes


def _reset_auth_caches() -> None:
    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    owner._fa_cache["value"] = None
    owner._fa_cache["checked_at"] = 0.0
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(auth_routes.router)

    @app.get("/landing")
    async def _landing() -> PlainTextResponse:
        return PlainTextResponse("MARKETING")

    @app.get("/private")
    async def _private() -> PlainTextResponse:
        return PlainTextResponse("PRIVATE")

    @app.get("/api/private")
    async def _api_private() -> dict[str, bool]:
        return {"private": True}

    return app


@pytest_asyncio.fixture
async def exclusive_setup(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    sent: list[str] = []

    async def _fake_send(to_addr: str, *_args, **_kwargs) -> dict[str, str]:
        sent.append(to_addr)
        return {"status": "sent"}

    monkeypatch.setattr(auth_routes, "send_email", _fake_send)
    monkeypatch.setattr(auth_routes, "_rate_allow", lambda *_a, **_kw: True)

    owner = await create_user("owner@example.test", "owner-pass-123")
    member = await create_user("member@example.test", "member-pass-123")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    await set_kv(db, "owner_exclusive_mode", "1")
    _reset_auth_caches()

    app = _app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db, owner, member, sent
    finally:
        # The shared DB fixture clears user rows but intentionally preserves
        # KV settings. Restore the opt-in flag and its module cache so this
        # scenario cannot affect unrelated legacy tests.
        await set_kv(db, "owner_exclusive_mode", "0")
        _reset_auth_caches()


@pytest.mark.asyncio
async def test_marketing_stays_public_and_private_html_api_fail_closed(exclusive_setup):
    client, _db, owner, member, _sent = exclusive_setup

    assert (await client.get("/landing")).status_code == 200

    html = await client.get("/private", follow_redirects=False)
    assert html.status_code == 303
    assert html.headers["location"] == "/landing"

    api = await client.get("/api/private")
    assert api.status_code == 401
    assert api.json()["detail"] == "authentication required"

    member_token, _ = await issue_session(member["id"])
    client.cookies.set(SESSION_COOKIE_NAME, member_token)
    html = await client.get("/private", follow_redirects=False)
    assert html.status_code == 303
    assert html.headers["location"] == "/pending"
    api = await client.get("/api/private")
    assert api.status_code == 403
    assert api.json()["detail"] == "owner access required"

    client.cookies.clear()
    owner_token, _ = await issue_session(owner["id"])
    client.cookies.set(SESSION_COOKIE_NAME, owner_token)
    assert (await client.get("/private")).status_code == 200
    assert (await client.get("/api/private")).status_code == 200


@pytest.mark.asyncio
async def test_signup_and_auto_register_are_disabled_without_creating_users(exclusive_setup):
    client, db, _owner, _member, sent = exclusive_setup

    page = await client.get("/auth/signup", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/auth/login"

    signup = await client.post(
        "/auth/signup",
        data={
            "email": "new@example.test",
            "password": "new-password-123",
            "display_name": "New",
        },
    )
    assert signup.status_code == 403
    assert "только владельцу" in signup.text

    register = await client.post(
        "/auth/register",
        data={"email": "another@example.test"},
        headers={"X-Requested-With": "fetch"},
    )
    assert register.status_code == 403
    assert register.json()["ok"] is False
    assert sent == []

    cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
    assert int((await cursor.fetchone())["n"]) == 2


@pytest.mark.asyncio
async def test_only_primary_owner_can_receive_a_new_login_session(exclusive_setup):
    client, db, owner, member, _sent = exclusive_setup

    login_page = await client.get("/auth/login")
    assert login_page.status_code == 200
    assert "Создать аккаунт" not in login_page.text

    owner_login = await client.post(
        "/auth/login",
        data={"email": owner["email"], "password": "owner-pass-123"},
        headers={"X-Requested-With": "fetch"},
    )
    assert owner_login.status_code == 200
    assert owner_login.json()["redirect"] == "/now"
    assert owner_login.cookies.get(SESSION_COOKIE_NAME)

    client.cookies.clear()
    member_login = await client.post(
        "/auth/login",
        data={"email": member["email"], "password": "member-pass-123"},
        headers={"X-Requested-With": "fetch"},
    )
    assert member_login.status_code == 401
    assert member_login.cookies.get(SESSION_COOKIE_NAME) is None

    cursor = await db.execute(
        "SELECT user_id FROM auth_session WHERE revoked_at IS NULL ORDER BY id"
    )
    active_user_ids = [int(row["user_id"]) for row in await cursor.fetchall()]
    assert active_user_ids == [owner["id"]]


@pytest.mark.asyncio
async def test_revoke_non_owner_sessions_preserves_owner_and_users(exclusive_setup):
    _client, db, owner, member, _sent = exclusive_setup
    another = await create_user("another@example.test", "another-pass-123")

    owner_tokens = [(await issue_session(owner["id"]))[0] for _ in range(2)]
    member_tokens = [(await issue_session(member["id"]))[0] for _ in range(2)]
    another_token, _ = await issue_session(another["id"])

    assert await count_active_non_owner_sessions(owner["id"]) == 3
    assert await revoke_non_owner_sessions(owner["id"]) == 3
    assert await revoke_non_owner_sessions(owner["id"]) == 0

    cursor = await db.execute(
        "SELECT token, user_id, revoked_at FROM auth_session ORDER BY id"
    )
    rows = {str(row["token"]): row for row in await cursor.fetchall()}
    assert all(rows[token]["revoked_at"] is None for token in owner_tokens)
    assert all(rows[token]["revoked_at"] is not None for token in member_tokens)
    assert rows[another_token]["revoked_at"] is not None

    cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
    assert int((await cursor.fetchone())["n"]) == 3


@pytest.mark.asyncio
async def test_exclusive_flag_lookup_failure_is_fail_closed(monkeypatch):
    async def _broken_lookup() -> bool:
        raise aiosqlite.OperationalError("database unavailable")

    monkeypatch.setattr(exclusive, "read_owner_exclusive_mode", _broken_lookup)
    assert await exclusive.owner_exclusive_enabled() is True


@pytest.mark.asyncio
async def test_lockdown_cli_does_not_replay_migrations_for_existing_db(
    exclusive_setup,
    monkeypatch: pytest.MonkeyPatch,
):
    _client, _db, _owner, _member, _sent = exclusive_setup

    async def _unexpected_init() -> None:
        raise AssertionError("existing production DB must not replay migrations")

    monkeypatch.setattr(lockdown, "init_database", _unexpected_init)
    assert await lockdown.run(confirm=False) == 0
