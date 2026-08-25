"""«Одолжить свою модель другу» — страница /settings/llm/sharing.

Что здесь можно
---------------
* «Я делюсь» — кому я выдал доступ: лимит, расход за сегодня, пауза, отзыв.
* «Мне дали доступ» — кто одолжил свою модель мне: имя провайдера (ТОЛЬКО
  имя), лимит и остаток. Ключ выдавшего сюда не приходит вообще — ни в
  шаблон, ни в контекст, ни в лог: :mod:`app.llm.grants` его не читает.
* Форма выдачи: ВЫБОР ОДНОГО ДРУГА из списка + дневной лимит + необязательная
  заметка. Никакого «выдать всем друзьям» — только поимённо.

Почему друг из списка, а не адрес
---------------------------------
Форма принимала точный e-mail, а подсказка под ней печатала адреса всех твоих
друзей — то есть достаточно было с человеком подружиться, чтобы забрать его
настоящую почту (включая почту владельца инстанса, которую весь остальной
продукт маскирует). Теперь в форме ездит ``friend_id``, а :mod:`app.llm.grants`
наружу отдаёт только ``{"id", "name"}``: адреса в этом потоке нет вообще —
ни в разметке, ни в POST-теле, ни в подтверждении. Заодно исчез оракул
«существует ли аккаунт с таким адресом»: выбрать можно только из своих друзей,
а выдача всё равно не работала до подтверждённой дружбы.

Почему лимит обязателен
-----------------------
Это чужие деньги (облачный ключ) или чужое железо (Ollama/ПК). Схема
``llm_grant`` запрещает ``daily_limit <= 0`` на уровне CHECK, форма
подставляет скромный дефолт, а резолвер списывает по единице на КАЖДУЮ сборку
клиента. Отозвать можно в любой момент, и отзыв действует с того же запроса.

Доступ к странице
-----------------
Гейт (``app/web/middleware/auth_gate.py``) уже пускает участника во всю зону
``/settings/llm`` — ``_is_member_path`` матчит ``p`` и ``p + "/"``, поэтому
``/settings/llm/sharing`` покрыт существующей записью ``"/settings/llm"``.
Отдельная запись не добавлялась намеренно: дублирующий префикс в
security-кортеже — это лишний шум ровно там, где важна читаемость.

Где живёт SQL
-------------
Здесь его нет вовсе: и выдачи, и резолв друга по id, и список друзей лежат в
:mod:`app.llm.grants`. Это не стилистика, а гейт —
``tests/test_architecture_gates.py`` запрещает новым роутам импортировать
``app.storage.db`` напрямую, чтобы SQL не расползался по HTTP-слою.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.llm import grants as grants_mod
from app.logging_setup import get_logger
from app.web.routes.owner_view import (  # Fail-closed резолв роли.
    viewer_is_owner as is_owner,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"], dependencies=[Depends(current_user_required)])
log = get_logger("persona.llm.sharing")

_PAGE = "/settings/llm/sharing"


async def _render(
    request: Request,
    user_id: int,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Единственная точка отрисовки — чтобы все ветки собирали один контекст.

    Ни одно поле здесь не содержит e-mail: :mod:`app.llm.grants` отдаёт людей
    как ``{"id", "name"}`` / ``*_name``, поэтому маскировать в роуте нечего.
    """
    issued = await grants_mod.list_issued_by(user_id)
    received = await grants_mod.list_received_by(user_id)
    own_provider = await grants_mod.grantor_provider_name(user_id)
    return templates.TemplateResponse(
        request,
        "llm_sharing.html",
        {
            "title": "Доступ к модели",
            "active_nav": "settings",
            "issued": issued,
            "received": received,
            "own_provider": own_provider,
            "friends": await grants_mod.friend_suggestions(user_id),
            "default_limit": grants_mod.DEFAULT_DAILY_LIMIT,
            "max_limit": grants_mod.MAX_DAILY_LIMIT,
            "is_owner": await is_owner(user_id),
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


@router.get(_PAGE, response_class=HTMLResponse, response_model=None)
async def sharing_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return await _render(request, int(session["user_id"]))


@router.post(_PAGE + "/grant", response_class=HTMLResponse, response_model=None)
async def create_grant(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    friend_id: Annotated[str, Form()] = "",
    daily_limit: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Выдать доступ ОДНОМУ выбранному другу (``friend_id``, не адрес)."""
    uid = int(session["user_id"])

    try:
        limit = int(str(daily_limit).strip() or grants_mod.DEFAULT_DAILY_LIMIT)
    except ValueError:
        return await _render(
            request, uid, error="Лимит — это число запросов в сутки.", status_code=400
        )
    if limit < 1 or limit > grants_mod.MAX_DAILY_LIMIT:
        return await _render(
            request,
            uid,
            error=f"Лимит должен быть от 1 до {grants_mod.MAX_DAILY_LIMIT} запросов в сутки.",
            status_code=400,
        )

    # ``friend_id`` приходит от клиента, поэтому резолвится по СПИСКУ ДРУЗЕЙ
    # вызывающего (grants.friend_for_grant), а не приводится к int «на веру».
    # Один и тот же текст ошибки и на «не выбрал», и на «выбрал чужой номер»:
    # разные формулировки превратили бы форму в оракул существования аккаунтов.
    target = await grants_mod.friend_for_grant(uid, friend_id)
    if target is None:
        return await _render(
            request,
            uid,
            error="Выбери, кому выдать доступ — из списка своих друзей.",
            status_code=400,
        )

    grant_id = await grants_mod.upsert_grant(uid, int(target["id"]), limit, note)
    await log_action(
        "llm.grant.create",
        target=str(target["id"]),
        detail=f"limit={limit}",
        success=True,
    )
    log.info("llm_grant.created", grantor=uid, grantee=int(target["id"]), limit=limit)

    friends_ready = await grants_mod.friends_confirmed(uid, int(target["id"]))
    # Называем человека ровно так же, как называл его список выбора: имя или
    # маска. Адреса у роута нет — и это теперь структурное свойство, а не
    # аккуратность вызывающего.
    notice = (
        f"Готово: {target['name']} может ходить в твою модель "
        f"до {limit} раз в сутки. Отозвать — кнопкой ниже, в любой момент."
    )
    if not friends_ready:
        notice += (
            " Но пока вы не подтверждённые друзья, доступ не заработает — "
            "добавьте друг друга в друзья."
        )
    log.debug("llm_grant.created_id", grant_id=grant_id)
    return await _render(request, uid, notice=notice)


@router.post(_PAGE + "/{grant_id}/limit", response_class=HTMLResponse, response_model=None)
async def update_limit(
    request: Request,
    grant_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    daily_limit: Annotated[str, Form()] = "",
) -> HTMLResponse:
    uid = int(session["user_id"])
    try:
        limit = int(str(daily_limit).strip())
    except ValueError:
        return await _render(request, uid, error="Лимит — это число.", status_code=400)
    if limit < 1 or limit > grants_mod.MAX_DAILY_LIMIT:
        return await _render(
            request,
            uid,
            error=f"Лимит должен быть от 1 до {grants_mod.MAX_DAILY_LIMIT}.",
            status_code=400,
        )
    ok = await grants_mod.set_limit(uid, grant_id, limit)
    if not ok:
        return await _render(request, uid, error="Такой выдачи у тебя нет.", status_code=404)
    await log_action("llm.grant.limit", target=str(grant_id), detail=str(limit), success=True)
    return await _render(request, uid, notice=f"Новый лимит: {limit} запросов в сутки.")


@router.post(_PAGE + "/{grant_id}/toggle", response_class=HTMLResponse, response_model=None)
async def toggle_grant(
    request: Request,
    grant_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    enabled: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Пауза/возобновление. Пауза — не отзыв: строка и её история остаются."""
    uid = int(session["user_id"])
    want = str(enabled).strip() in ("1", "true", "on", "yes")
    ok = await grants_mod.set_enabled(uid, grant_id, want)
    if not ok:
        return await _render(request, uid, error="Такой выдачи у тебя нет.", status_code=404)
    await log_action(
        "llm.grant.toggle", target=str(grant_id), detail="on" if want else "off", success=True
    )
    return await _render(
        request,
        uid,
        notice="Доступ включён." if want else "Доступ поставлен на паузу.",
    )


@router.post(_PAGE + "/{grant_id}/revoke", response_class=HTMLResponse, response_model=None)
async def revoke_grant(
    request: Request,
    grant_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    uid = int(session["user_id"])
    ok = await grants_mod.revoke(uid, grant_id)
    if not ok:
        return await _render(request, uid, error="Такой выдачи у тебя нет.", status_code=404)
    await log_action("llm.grant.revoke", target=str(grant_id), success=True)
    log.info("llm_grant.revoked", grantor=uid, grant_id=grant_id)
    return await _render(request, uid, notice="Доступ отозван. Работает с этой секунды.")


@router.get(_PAGE + "/", response_class=RedirectResponse, response_model=None)
async def sharing_slash() -> RedirectResponse:
    """Хвостовой слэш → канонический путь (иначе 404 на копипасте из адресной строки)."""
    return RedirectResponse(url=_PAGE, status_code=308)
