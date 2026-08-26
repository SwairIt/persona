"""Инструменты сравнения скриншотов доступны ТОЛЬКО владельцу.

Почему этот файл существует
---------------------------
``/compare`` попал в ``_PUBLIC_PREFIXES`` гейта (совпадение по префиксу), а сами
роуты требовали лишь ``current_user_required`` — любую сессию. Скриншоты при
этом лежат в ГЛОБАЛЬНОЙ таблице ``screenshots`` без колонки пользователя и
грузятся по «голому» id. В сумме: любой зарегистрировавшийся человек мог
перебором id смотреть экран владельца и распознанный с него текст.

Аудит изоляции это пропустил, потому что перечислял ``_MEMBER_PREFIXES``, —
а дыра жила в списке ПУБЛИЧНЫХ путей. Отсюда правило, которое сторожит тест:
роут, отдающий данные захвата, обязан проверять владельца САМ, независимо от
того, что решил гейт.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.repository import set_kv
from app.web.routes import shot_compare as shot_compare_routes
from app.web.routes import side_by_side as side_by_side_routes

# Гейт здесь НЕ подключаем намеренно: проверяем, что защищает сам роут.
# Если однажды кто-то снова расширит публичный префикс, тест не заметит
# изменения гейта — и именно поэтому он останется зелёным только пока
# внутренняя проверка владельца на месте.
CAPTURE_COMPARE_PATHS = (
    "/compare?a=1&b=2",
    "/api/compare.json?a=1&b=2",
    "/compare/1/2",
    "/api/compare/shots-of-day.json",
)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(shot_compare_routes.router)
    app.include_router(side_by_side_routes.router)
    return app


@pytest_asyncio.fixture
async def compare_setup(db: aiosqlite.Connection):
    owner_user = await create_user("owner@compare.test", "Zq7-frost-lantern-91")
    member_user = await create_user("member@compare.test", "Kp4-velvet-harbour-38")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    owner._fa_cache["value"] = None
    owner._fa_cache["checked_at"] = 0.0

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        yield client, owner_user, member_user


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


@pytest.mark.asyncio
async def test_member_cannot_reach_capture_compare(compare_setup):
    """Участник не должен получить ни страницу, ни JSON с чужим скриншотом."""
    client, _owner_user, member_user = compare_setup
    await _as(client, member_user["id"])
    for path in CAPTURE_COMPARE_PATHS:
        r = await client.get(path, follow_redirects=False)
        assert r.status_code in (403, 404), f"{path} отдал участнику {r.status_code}"


@pytest.mark.asyncio
async def test_anonymous_cannot_reach_capture_compare(compare_setup):
    """Без сессии — тем более."""
    client, _owner_user, _member_user = compare_setup
    client.cookies.clear()
    for path in CAPTURE_COMPARE_PATHS:
        r = await client.get(path, follow_redirects=False)
        assert r.status_code != 200, f"{path} отдал 200 анониму"
