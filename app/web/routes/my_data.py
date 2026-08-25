"""«Мои данные» — /settings/my-data. Права участника на СВОИ данные (152-ФЗ).

Почему это ОТДЕЛЬНАЯ страница, а не открытый участнику ``/settings/privacy``
──────────────────────────────────────────────────────────────────────────────
``/settings/privacy`` — это пульт ВЛАДЕЛЬЦА инстанса, и его подроуты не
масштабируются на участника:

* ``GET /settings/privacy/snapshot`` делает ``VACUUM INTO`` по **всей базе** и
  отдаёт файл вызывающему. Это не «мои данные» — это все чаты всех аккаунтов,
  скриншоты и OCR владельца, kv-секреты (SMTP-пароль, токен Telegram-бота,
  ключи API). Открыть префикс участнику = выдать ему дамп инстанса одной
  ссылкой. Никакой «пофайловой фильтрации» тут не бывает: артефакт по своей
  природе инстанс-глобальный.
* ``GET /settings/privacy/export-memory`` формально per-user, но живёт на той
  же странице и в той же навигации.
* ``POST /settings/privacy/wipe-memory`` и остальные тумблеры страницы
  (ретеншен, редактирование, «стереть всё») — инстанс-глобальные и для
  участника бессмысленны.

Поэтому выбран вариант (b) из брифа: ``/settings/privacy`` остаётся owner-only
и в member-префиксы НЕ добавляется, а участник получает собственный экран.
Дополнительно опасные подроуты владельческой страницы закрыты **явной**
owner-зависимостью (защита в глубину, см. app/web/routes/privacy_settings.py):
даже если когда-нибудь префикс откроют по ошибке, снимок базы не уедет.

Что тут есть
────────────
* ``GET  /settings/my-data``              — экран со счётчиками и кнопками;
* ``GET  /settings/my-data/export.json``  — вся выгрузка одним JSON;
* ``GET  /settings/my-data/export.zip``   — тот же JSON внутри zip;
* ``POST /settings/my-data/delete``       — удаление аккаунта с подтверждением.

SQL тут отсутствует принципиально: архитектурный гейт запрещает новым модулям
в ``app/web/routes`` импортировать ``app.storage.db``. Вся работа с базой —
за портом :mod:`app.auth.data_rights` (он же тянет :mod:`app.auth.data_export`
и :mod:`app.auth.account_delete`), обработчики только рендерят результат.

Инвариант «нельзя удалить чужой аккаунт»
────────────────────────────────────────
Ни один обработчик здесь **не принимает id пользователя ни в каком виде** —
ни в пути, ни в query, ни в форме, ни в JSON. Единственный источник личности —
cookie-сессия (``current_user_required``). Лишние поля в теле просто
игнорируются FastAPI: подставить чужой uid физически некуда.
"""

from __future__ import annotations

import io
import zipfile
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import SESSION_COOKIE_NAME, current_user_required
from app.auth.account_delete import REFUSE_OWNER, delete_own_account
from app.auth.data_export import build_export, export_json_bytes
from app.auth.data_rights import confirmation_matches, summary, summary_context
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.web import rate_limit
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.my_data")

_REFUSAL_TEXT: dict[str, str] = {
    REFUSE_OWNER: (
        "Аккаунт владельца инстанса так удалить нельзя — на нём держится доступ "
        "ко всей установке. Если нужно закрыть инстанс, это делается на сервере."
    ),
    "user_not_found": "Аккаунт не найден.",
    "confirmation_mismatch": (
        "Подтверждение не совпало. Впиши свой e-mail ровно так, как он показан выше."
    ),
    "rate_limited": "Слишком много попыток. Подожди немного и попробуй снова.",
}


async def _page_context(user_id: int, *, error: str | None = None) -> dict[str, object]:
    """Контекст шаблона. Данные приходят из порта app.auth.data_rights — SQL тут нет."""
    data = await summary(user_id)
    return {
        "title": "Мои данные",
        "active_nav": "settings",
        "error": error,
        **summary_context(data, _REFUSAL_TEXT),
    }


@router.get("/settings/my-data", response_class=HTMLResponse)
async def my_data_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Экран «Мои данные»: что о тебе хранится, скачать, удалить аккаунт."""
    return templates.TemplateResponse(
        request, "my_data.html", await _page_context(int(session["user_id"]))
    )


@router.get("/settings/my-data/export.json")
async def export_json(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> Response:
    """Полная выгрузка ТЕКУЩЕГО пользователя. Секреты редактируются."""
    uid = int(session["user_id"])
    payload = await build_export(uid)
    log.info("my_data.export", user_id=uid, fmt="json")
    return Response(
        content=export_json_bytes(payload),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="persona-export-{uid}.json"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/settings/my-data/export.zip")
async def export_zip(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> Response:
    """Тот же JSON, упакованный в zip — так его удобнее хранить и передавать."""
    uid = int(session["user_id"])
    payload = await build_export(uid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"persona-export-{uid}.json", export_json_bytes(payload))
        zf.writestr(
            "README.txt",
            "Выгрузка данных Persona.\n"
            "Внутри — persona-export-<id>.json: всё, что сервис хранит об этом\n"
            "аккаунте. Ключи API, токены ботов и токены сессий заменены на\n"
            "маркер: отдавать действующие учётные данные файлом небезопасно.\n"
            "Адреса других людей показаны маской — это их данные, не твои.\n",
        )
    log.info("my_data.export", user_id=uid, fmt="zip")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="persona-export-{uid}.zip"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/settings/my-data/delete", response_model=None)
async def delete_account(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    confirm: str = Form(default=""),
) -> Response:
    """Удалить СВОЙ аккаунт. Личность берётся из сессии, из тела — никогда.

    Подтверждение: в поле ``confirm`` надо вписать собственный e-mail (или
    ``app.auth.data_rights.CONFIRM_PHRASE``, если адреса нет). Любые лишние поля
    формы (``user_id``, ``uid``, …) не объявлены в сигнатуре и игнорируются —
    удалить чужой аккаунт этим роутом невозможно by construction.

    Личные сообщения удаляются у ОБЕИХ сторон — см. ``app/auth/account_delete``.
    """
    uid = int(session["user_id"])

    if not rate_limit.allow(f"account_delete:{uid}", 5, 3600):
        return templates.TemplateResponse(
            request,
            "my_data.html",
            await _page_context(uid, error=_REFUSAL_TEXT["rate_limited"]),
            status_code=429,
        )

    if not await confirmation_matches(uid, confirm):
        return templates.TemplateResponse(
            request,
            "my_data.html",
            await _page_context(uid, error=_REFUSAL_TEXT["confirmation_mismatch"]),
            status_code=400,
        )

    result = await delete_own_account(uid)
    if not result.ok:
        return templates.TemplateResponse(
            request,
            "my_data.html",
            await _page_context(
                uid,
                error=_REFUSAL_TEXT.get(
                    result.reason or "", "Удалить аккаунт не удалось."
                ),
            ),
            status_code=403,
        )

    response = RedirectResponse(url="/landing?deleted=1", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


__all__ = ["router"]
