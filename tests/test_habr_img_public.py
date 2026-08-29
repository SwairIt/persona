"""Скриншоты статьи отдаются по прямой ссылке и без входа.

Редактор Хабра в markdown-режиме ждёт готовый URL: `![подпись](адрес)`.
Картинки лежат в репозитории (`docs/habr-screenshots/`), поэтому проще
отдать их наружу самим, чем заводить отдельный хостинг. Так же сделано на
getdoday.ru — там `/habr-img/{name}` появился ровно под ту же задачу.

Инварианты:

* аноним получает картинку (Хабр ходит за ней без куки — если гейт
  завернёт запрос на /landing, в статье будет битая картинка);
* имя файла проверяется по строгому шаблону, обход каталога невозможен;
* несуществующее имя — 404, а не 500 и не редирект.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth.users import create_user
from app.storage.repository import set_kv
from app.web.main import create_app
from app.web.routes import habr_img, setup_gate

REAL_IMAGE = "01-landing.jpg"


@pytest_asyncio.fixture
async def client(db: aiosqlite.Connection):
    """Инстанс с ЗАРЕГИСТРИРОВАННЫМ владельцем — то есть гейт активен."""
    owner = await create_user("owner@habrimg.test", "Zq7-frost-lantern-91")
    await set_kv(db, "setup_complete", "true")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    await db.commit()
    setup_gate._cache.mark_done()
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def test_the_screenshot_folder_is_where_the_route_looks() -> None:
    """Тест ниже проверяет реальный файл — убедимся, что он на месте."""
    assert (habr_img.SCREENSHOT_DIR / REAL_IMAGE).is_file(), (
        f"нет {REAL_IMAGE} в {habr_img.SCREENSHOT_DIR}"
    )


@pytest.mark.asyncio
async def test_anonymous_visitor_gets_the_image(client: AsyncClient) -> None:
    """Без куки — картинка, а не редирект на /landing."""
    response = await client.get(f"/habr-img/{REAL_IMAGE}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content[:2] == b"\xff\xd8"  # JPEG SOI
    assert "max-age" in response.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_unknown_name_is_a_plain_404(client: AsyncClient) -> None:
    response = await client.get("/habr-img/00-does-not-exist.jpg")

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "../../.env",
        "..%2F..%2F.env",
        "01-landing.jpg/../../../app/__init__.py",
        "persona.db",
        "01-landing.png",
    ],
)
async def test_path_traversal_and_foreign_files_are_refused(
    client: AsyncClient, name: str
) -> None:
    """Картинкой отдаётся ТОЛЬКО jpg из папки скриншотов.

    Проверяем именно это, а не конкретный код ответа: часть таких адресов
    ещё до роутера схлопывается нормализацией URL (``/habr-img/../../.env``
    превращается в ``/.env``), уходит к гейту и получает 303 на /landing.
    Это нормальный отказ — важно, что содержимое чужого файла наружу не
    уходит ни при каком из путей.
    """
    response = await client.get(f"/habr-img/{name}")

    served_as_image = response.status_code == 200 and response.headers.get(
        "content-type", ""
    ).startswith("image/")
    assert not served_as_image, f"{name} отдался картинкой ({len(response.content)} байт)"
