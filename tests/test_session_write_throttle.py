"""Каждый авторизованный запрос не должен стоить записи в SQLite.

Замер до фикса (Windows, WAL, локальный uvicorn): ``GET /static/csrf.js``
с кукой сессии — ~55 мс, без куки — ~14 мс. Разница целиком уходила в
``verify_session()``: SELECT + UPDATE ``last_seen_at`` + COMMIT. Страница
кабинета тянет ~40 статик-файлов, то есть ~1.6 с серверной работы на
загрузку — и всё ради того, чтобы 40 раз переписать один и тот же
таймстамп.

Здесь два инварианта:

* ``/static/*`` вообще не трогает таблицу сессий (личность статике не нужна);
* ``last_seen_at`` пишется не чаще раза в минуту.

Точность «последняя активность» при этом падает максимум на минуту, а
idle-окно по умолчанию — 14 дней (``PERSONA_SESSION_IDLE_DAYS``), так что
на выселение простаивающих сессий троттлинг не влияет.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport, AsyncClient

from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session, verify_session
from app.auth.users import create_user
from app.storage.db import get_connection
from app.storage.repository import set_kv
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware


def _reset_auth_caches() -> None:
    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    owner._fa_cache["value"] = None
    owner._fa_cache["checked_at"] = 0.0
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0


async def _last_seen(token: str) -> str:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT last_seen_at FROM auth_session WHERE token = ?", (token,)
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row["last_seen_at"])


async def _set_last_seen(token: str, when: datetime) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE auth_session SET last_seen_at = ? WHERE token = ?",
            (when.isoformat(), token),
        )
        await conn.commit()


@pytest_asyncio.fixture
async def signed_in(db: aiosqlite.Connection):
    """Владелец с живой сессией + приложение с гейтом и статик-роутом."""
    user = await create_user("owner@example.test", "Zq7-frost-lantern-91")
    await set_kv(db, "owner_user_id", str(user["id"]))
    _reset_auth_caches()
    token, _expires = await issue_session(user["id"])

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)

    @app.get("/static/{path:path}")
    async def _static(path: str) -> PlainTextResponse:
        return PlainTextResponse(f"asset:{path}")

    @app.get("/private")
    async def _private() -> PlainTextResponse:
        return PlainTextResponse("PRIVATE")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(SESSION_COOKIE_NAME, token)
        yield client, token


@pytest.mark.asyncio
async def test_static_request_does_not_touch_session_table(signed_in) -> None:
    """Статика с кукой сессии не переписывает ``last_seen_at``."""
    client, token = signed_in
    stale = datetime.now(timezone.utc) - timedelta(days=1)
    await _set_last_seen(token, stale)

    response = await client.get("/static/csrf.js?v=2.35.0")

    assert response.status_code == 200
    assert await _last_seen(token) == stale.isoformat()


@pytest.mark.asyncio
async def test_page_request_still_records_activity(signed_in) -> None:
    """Обычная страница по-прежнему обновляет «последнюю активность»."""
    client, token = signed_in
    stale = datetime.now(timezone.utc) - timedelta(days=1)
    await _set_last_seen(token, stale)

    response = await client.get("/private")

    assert response.status_code == 200
    assert await _last_seen(token) != stale.isoformat()


@pytest.mark.asyncio
async def test_repeat_verify_within_a_minute_skips_the_write(db) -> None:
    """Второй ``verify_session`` за минуту не пишет в базу повторно."""
    user = await create_user("member@example.test", "Kp4-velvet-harbour-38")
    _reset_auth_caches()
    token, _expires = await issue_session(user["id"])
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    await _set_last_seen(token, recent)

    assert await verify_session(token) is not None

    assert await _last_seen(token) == recent.isoformat()


@pytest.mark.asyncio
async def test_verify_writes_again_once_the_window_passed(db) -> None:
    """Через минуту простоя активность снова записывается."""
    user = await create_user("member2@example.test", "Kp4-velvet-harbour-38")
    _reset_auth_caches()
    token, _expires = await issue_session(user["id"])
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    await _set_last_seen(token, old)

    assert await verify_session(token) is not None

    assert await _last_seen(token) != old.isoformat()
