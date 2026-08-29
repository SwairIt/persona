"""Скриншоты статьи по прямой ссылке — ``/habr-img/{name}``.

Зачем отдельный роут, а не ``/static/``: редактор Хабра в markdown-режиме
принимает только готовый адрес (``![подпись](url)``), а картинки к статье
живут в репозитории рядом с текстом (``docs/habr-screenshots/``), не в
статике приложения. Тащить их в ``app/web/static/`` только ради публикации
значит держать две копии и следить, чтобы они не разъехались.

Тот же приём и по тому же адресу работает на getdoday.ru.

Путь публичный намеренно: за картинкой в статье Хабр (и любой читатель)
ходит без куки. Поэтому ``/habr-img/`` внесён в ``_PUBLIC_PREFIXES`` и в
``_ASSET_PREFIXES`` гейта — второе, чтобы запрос не резолвил личность и не
трогал БД, как и любая другая раздача файлов.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from app.logging_setup import get_logger

log = get_logger("persona.web.habr_img")

router = APIRouter(tags=["habr-img"])

#: ``app/web/routes/habr_img.py`` → корень репозитория.
SCREENSHOT_DIR = Path(__file__).resolve().parents[3] / "docs" / "habr-screenshots"

#: Ровно то, что лежит в папке: ``01-landing.jpg``, ``01b-landing-features.jpg``,
#: ``11-theme-cosmos-dark.jpg``. Ни слэшей, ни точек, ни расширений кроме jpg —
#: обход каталога не выразим в этом алфавите.
_NAME = re.compile(r"[0-9]{2}[a-z]?-[a-z0-9-]+\.jpg")

#: Сутки: файлы к опубликованной статье не меняются, но и вечности не нужно —
#: если картинку придётся переснять, читатель увидит новую на следующий день.
_CACHE = "public, max-age=86400"


@router.get("/habr-img/{name}", include_in_schema=False)
async def habr_screenshot(name: str) -> Response:
    """Отдать скриншот статьи. 404 на всё, что не подходит под шаблон."""
    if not _NAME.fullmatch(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    path = (SCREENSHOT_DIR / name).resolve()
    # Пояс поверх подтяжек: даже если шаблон однажды ослабят, наружу не уйдёт
    # ничего за пределами папки со скриншотами.
    if not path.is_file() or SCREENSHOT_DIR.resolve() not in path.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": _CACHE},
    )
