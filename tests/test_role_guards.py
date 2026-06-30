"""Роли в auth_gate + require_role guard (СЛАЙС F6-12).

ВЫСОКИЙ РИСК: живой access-control. Поэтому слайс СТРОГО АДДИТИВЕН и ЗА
фича-флагом ``kv role_gate_enabled`` (DEFAULT OFF). Тесты делятся на:

  (а) КРИТ-РЕГРЕСС — при флаге OFF (дефолт) доступ владельца и не-владельца-Pro
      ТОЧНО как сейчас (деплой ничего не меняет);
  (б) require_role — при ВКЛ admin проходит admin-путь, member ловит 403,
      viewer пускается только на GET;
  (в) role_rank / иерархия viewer<member<admin<owner (owner — суперсет);
  (г) владелец НИКОГДА не теряет доступ (и при ВКЛ роле-гейте — всё видит).
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport, AsyncClient

from app.auth.guards import require_role, role_at_least
from app.auth.roles import role_rank
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.billing import service
from app.storage.db import init_database
from app.storage.repository import set_kv


@pytest.fixture(autouse=True)
def _reset_caches():
    """Сброс in-process кэшей (owner / role / gate / role-flag) между тестами."""
    from app.auth import guards, owner
    from app.web.middleware import auth_gate

    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    guards._invalidate_role_cache()
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._role_gate_cache["value"] = False
    auth_gate._role_gate_cache["checked_at"] = 0.0
    yield
    guards._invalidate_role_cache()


async def _add_user(db: aiosqlite.Connection, email: str, role: str = "member") -> int:
    cur = await db.execute(
        "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
        (email, "x", role),
    )
    await db.commit()
    return int(cur.lastrowid)


async def _enable_role_gate(db: aiosqlite.Connection) -> None:
    await set_kv(db, "role_gate_enabled", "1")
    # сбросить кэш флага, чтобы middleware перечитал его сразу
    from app.web.middleware import auth_gate

    auth_gate._role_gate_cache["checked_at"] = 0.0


# ──────────────────────────── (в) role_rank / иерархия ──────────────────────────


def test_role_rank_hierarchy():
    """viewer < member < admin < owner; неизвестная роль → 0 (ниже всех)."""
    assert role_rank("viewer") < role_rank("member") < role_rank("admin") < role_rank("owner")
    assert role_rank(None) == 0
    assert role_rank("nonsense") == 0
    # owner — старший ранг (суперсет)
    assert role_rank("owner") == max(
        role_rank("viewer"), role_rank("member"), role_rank("admin"), role_rank("owner")
    )


@pytest.mark.asyncio
async def test_role_at_least(db):
    """role_at_least: owner проходит любой порог, viewer — только viewer."""
    owner_id = await _add_user(db, "o@ex.io", role="owner")
    admin_id = await _add_user(db, "a@ex.io", role="admin")
    viewer_id = await _add_user(db, "v@ex.io", role="viewer")

    assert await role_at_least(owner_id, "admin") is True
    assert await role_at_least(owner_id, "owner") is True
    assert await role_at_least(admin_id, "member") is True
    assert await role_at_least(admin_id, "owner") is False
    assert await role_at_least(viewer_id, "member") is False
    assert await role_at_least(viewer_id, "viewer") is True
    # None (нет сессии) → viewer, ниже member
    assert await role_at_least(None, "member") is False


# ──────────────────────────── (б) require_role guard ────────────────────────────


def _guard_app() -> FastAPI:
    """Мини-приложение БЕЗ middleware: проверяем именно зависимость require_role."""
    app = FastAPI()

    @app.get("/admin/thing", dependencies=[Depends(require_role("admin"))])
    async def _admin_thing():
        return PlainTextResponse("ADMIN-OK")

    @app.post("/member/do", dependencies=[Depends(require_role("member"))])
    async def _member_do():
        return PlainTextResponse("MEMBER-OK")

    return app


@pytest.mark.asyncio
async def test_require_role_admin_passes_member_blocked(db):
    """require_role('admin'): admin → 200, member → 403, owner → 200 (суперсет)."""
    from app.auth import guards

    owner_id = await _add_user(db, "o@ex.io", role="owner")
    admin_id = await _add_user(db, "a@ex.io", role="admin")
    member_id = await _add_user(db, "m@ex.io", role="member")

    app = _guard_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async def hit(uid: int, method: str, path: str):
            guards._invalidate_role_cache()
            ac.cookies.clear()
            token, _ = await issue_session(uid)
            ac.cookies.set(SESSION_COOKIE_NAME, token)
            return await ac.request(method, path, follow_redirects=False)

        assert (await hit(admin_id, "GET", "/admin/thing")).status_code == 200
        assert (await hit(owner_id, "GET", "/admin/thing")).status_code == 200
        # member не дотягивает до admin → 403
        assert (await hit(member_id, "GET", "/admin/thing")).status_code == 403


@pytest.mark.asyncio
async def test_require_role_no_session_redirects(db):
    """Нет сессии → current_user_required даёт 303 на логин (а не 403/200)."""
    app = _guard_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/admin/thing", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"


# ──────────────────────── (б+г) роле-гейт в middleware ──────────────────────────


def _gate_app() -> FastAPI:
    from app.web.middleware.auth_gate import AuthGateMiddleware

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)

    @app.get("/now")
    async def _now():
        return PlainTextResponse("NOW")

    @app.get("/admin/panel")
    async def _admin_panel():
        return PlainTextResponse("ADMIN")

    @app.post("/admin/panel")
    async def _admin_panel_post():
        return PlainTextResponse("ADMIN-POST")

    @app.get("/root")
    async def _root():
        return PlainTextResponse("ROOT")

    @app.get("/chat")
    async def _chat():
        return PlainTextResponse("CHAT")

    return app


async def _hit(ac: AsyncClient, uid: int, method: str, path: str):
    ac.cookies.clear()
    token, _ = await issue_session(uid)
    ac.cookies.set(SESSION_COOKIE_NAME, token)
    return await ac.request(method, path, follow_redirects=False)


@pytest.mark.asyncio
async def test_gate_role_on_admin_member_viewer(db):
    """Флаг ON: admin видит /admin; member — нет (→ /billing); viewer — GET-only."""
    owner_id = await _add_user(db, "o@ex.io", role="owner")   # id=1 → владелец (MIN id)
    admin_id = await _add_user(db, "a@ex.io", role="admin")
    member_id = await _add_user(db, "m@ex.io", role="member")
    viewer_id = await _add_user(db, "v@ex.io", role="viewer")
    await _enable_role_gate(db)

    app = _gate_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # admin → /admin/* пускают, /root (зона владельца) — нет
        assert (await _hit(ac, admin_id, "GET", "/admin/panel")).status_code == 200
        r = await _hit(ac, admin_id, "GET", "/root")
        assert r.status_code == 303 and r.headers["location"] == "/billing"
        # admin → обычное приложение тоже видит
        assert (await _hit(ac, admin_id, "GET", "/now")).status_code == 200

        # member → приложение видит, но НЕ /admin/*
        assert (await _hit(ac, member_id, "GET", "/now")).status_code == 200
        r = await _hit(ac, member_id, "GET", "/admin/panel")
        assert r.status_code == 303 and r.headers["location"] == "/billing"

        # viewer → только безопасные (GET) методы; POST → отказ (fallback /billing)
        assert (await _hit(ac, viewer_id, "GET", "/now")).status_code == 200
        r = await _hit(ac, viewer_id, "POST", "/admin/panel")
        assert r.status_code == 303 and r.headers["location"] == "/billing"

        # владелец → ВСЁ, и при ВКЛ роле-гейте (суперсет: /root, /admin, /now)
        assert (await _hit(ac, owner_id, "GET", "/root")).status_code == 200
        assert (await _hit(ac, owner_id, "GET", "/admin/panel")).status_code == 200
        assert (await _hit(ac, owner_id, "GET", "/now")).status_code == 200


# ──────────────────── (а) КРИТ-РЕГРЕСС: флаг OFF = старое поведение ──────────────


@pytest.mark.asyncio
async def test_gate_flag_off_identical_to_owner_gate(db):
    """Флаг OFF (дефолт): владелец → всё; Pro-подписчик → только /chat, /now → /billing;
    без подписки → /billing. Т.е. БАЙТ-В-БАЙТ как owner-gate сейчас."""
    owner_id = await _add_user(db, "o@ex.io", role="owner")   # id=1 → владелец
    sub_id = await _add_user(db, "s@ex.io", role="member")     # подписчик
    free_id = await _add_user(db, "f@ex.io", role="member")    # без подписки
    await service.grant_pro(sub_id, 30)
    # role_gate_enabled НЕ установлен → дефолт OFF

    app = _gate_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # владелец → всё
        assert (await _hit(ac, owner_id, "GET", "/now")).status_code == 200
        assert (await _hit(ac, owner_id, "GET", "/chat")).status_code == 200
        # подписчик → /chat можно, /now нельзя (→ /billing)
        assert (await _hit(ac, sub_id, "GET", "/chat")).status_code == 200
        r = await _hit(ac, sub_id, "GET", "/now")
        assert r.status_code == 303 and r.headers["location"] == "/billing"
        # без подписки → всё в /billing
        r = await _hit(ac, free_id, "GET", "/chat")
        assert r.status_code == 303 and r.headers["location"] == "/billing"


@pytest.mark.asyncio
async def test_gate_flag_off_admin_role_has_no_extra_access(db):
    """КРИТ: при OFF роль admin НЕ даёт доступа сверх owner-gate (флаг реально гейтит).

    admin-роль без подписки при OFF не должен видеть ни /admin, ни /now —
    т.е. дефолт-деплой не меняет ни одного решения доступа."""
    await _add_user(db, "o@ex.io", role="owner")               # id=1 → владелец
    admin_id = await _add_user(db, "a@ex.io", role="admin")     # admin, но флаг OFF

    app = _gate_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await _hit(ac, admin_id, "GET", "/admin/panel")
        assert r.status_code == 303 and r.headers["location"] == "/billing"
        r = await _hit(ac, admin_id, "GET", "/now")
        assert r.status_code == 303 and r.headers["location"] == "/billing"


@pytest.mark.asyncio
async def test_gate_owner_never_loses_access_flag_on(db):
    """(г) Владелец не теряет доступ при ВКЛ флаге даже с ролью != owner в строке.

    owner-gate (kv owner_user_id / MIN id) — суперсет независимо от users.role.
    Здесь владелец по owner-gate, но строковая роль 'member' → всё равно всё видит."""
    owner_id = await _add_user(db, "o@ex.io", role="member")   # id=1 → owner по MIN id
    await _enable_role_gate(db)

    app = _gate_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        assert (await _hit(ac, owner_id, "GET", "/root")).status_code == 200
        assert (await _hit(ac, owner_id, "GET", "/admin/panel")).status_code == 200
        assert (await _hit(ac, owner_id, "GET", "/now")).status_code == 200
