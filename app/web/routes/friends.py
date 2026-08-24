"""Друзья — /friends + /api/friends/*.

Страница: поиск людей, входящие заявки (принять/отклонить), исходящие
(отменить), список друзей («написать» / «удалить из друзей») и тумблер
«меня можно найти по поиску».

Весь SQL — в :mod:`app.social.repository`; здесь только HTTP, валидация
входа и rate-limit. Каждая функция репозитория и так фильтрует по id
действующего пользователя, поэтому «сырой» id из URL никуда не уезжает
без проверки владения.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.social.notifications import notify_friend_accepted, notify_friend_request
from app.social.repository import (
    NAME_MIN_CHARS,
    SEARCH_LIMIT_MAX,
    SocialError,
    accept_request,
    are_friends,
    cancel_request,
    decline_request,
    is_discoverable,
    list_friends,
    list_incoming,
    list_outgoing,
    search_users,
    send_request,
    set_discoverable,
    unfriend,
)
from app.web.rate_limit import allow
from app.web.templates_engine import templates

router = APIRouter(tags=["social"])
log = get_logger("persona.social.friends")

# Поиск людей — самый «перечислимый» эндпоинт социалки, поэтому у него свой
# потолок: 30 запросов в минуту на аккаунт. Живому человеку, который печатает
# в live-поиске, этого с запасом; скрипту, который перебирает адреса, — нет.
_SEARCH_MAX_EVENTS = 30
_SEARCH_WINDOW_SECONDS = 60
# Заявки — ещё жёстче: 20 в час. Спам «добавься ко мне» не должен масштабироваться.
_REQUEST_MAX_EVENTS = 20
_REQUEST_WINDOW_SECONDS = 3600


@router.get("/friends", response_class=HTMLResponse)
async def friends_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    uid = int(session["user_id"])
    return templates.TemplateResponse(
        request,
        "friends.html",
        {
            "title": "Друзья",
            "active_nav": "friends",
            "incoming": await list_incoming(uid),
            "outgoing": await list_outgoing(uid),
            "friends": await list_friends(uid),
            "discoverable": await is_discoverable(uid),
            "name_min_chars": NAME_MIN_CHARS,
            "search_limit": SEARCH_LIMIT_MAX,
        },
    )


@router.get("/api/friends/search", response_class=JSONResponse)
async def api_friends_search(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    q: str = "",
) -> JSONResponse:
    """Живой поиск людей. Возвращает id/имя/статус — НИКОГДА e-mail."""
    uid = int(session["user_id"])
    if not allow(f"social:search:{uid}", _SEARCH_MAX_EVENTS, _SEARCH_WINDOW_SECONDS):
        return JSONResponse(
            {"error": "слишком часто, подожди минуту", "results": []},
            status_code=429,
        )
    return JSONResponse({"results": await search_users(q, uid)})


@router.post("/api/friends/request", response_class=JSONResponse)
async def api_friends_request(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """Отправить заявку в друзья (``to_user_id``, необязательный ``message``)."""
    uid = int(session["user_id"])
    if not allow(f"social:request:{uid}", _REQUEST_MAX_EVENTS, _REQUEST_WINDOW_SECONDS):
        return JSONResponse({"error": "слишком много заявок, притормози"}, status_code=429)
    raw = payload.get("to_user_id")
    try:
        to_user_id = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return JSONResponse({"error": "не указан получатель"}, status_code=400)
    try:
        request_id = await send_request(uid, to_user_id, str(payload.get("message") or ""))
    except SocialError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    log.info("social.request_sent", from_user=uid, to_user=to_user_id)
    # Встречная заявка = взаимность: ``send_request`` в этом случае сразу
    # заводит дружбу, и «тебе пришла заявка» было бы враньём — адресату надо
    # сказать «твою заявку приняли».
    mutual = await are_friends(uid, to_user_id)
    event = (
        notify_friend_accepted(to_user_id, uid)
        if mutual
        else notify_friend_request(to_user_id, uid, str(payload.get("message") or ""))
    )
    # Уведомление адресату — в фоне: доставка (почта/telegram) идёт по сети,
    # а отправитель не должен ждать чужой SMTP, чтобы увидеть «отправлено».
    asyncio.create_task(  # noqa: RUF006 — fire-and-forget
        _notify_quietly(event), name=f"social-notify-request-{to_user_id}"
    )
    return JSONResponse({"ok": True, "request_id": request_id})


async def _notify_quietly(coro: Any) -> None:
    """Фоновая доставка уведомления: сбой логируем, наружу не выпускаем."""
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        log.warning("social.notify_failed", error=str(exc)[:200])


@router.post("/api/friends/{request_id}/accept", response_class=JSONResponse)
async def api_friends_accept(
    request_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    uid = int(session["user_id"])
    # Кому уходит «твою заявку приняли», знаем ДО принятия: после апдейта
    # заявка уже не ``pending`` и автора пришлось бы искать отдельно.
    author = next(
        (r["user_id"] for r in await list_incoming(uid) if r["id"] == int(request_id)),
        None,
    )
    ok = await accept_request(request_id, uid)
    if ok and author is not None:
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget
            _notify_quietly(notify_friend_accepted(int(author), uid)),
            name=f"social-notify-accept-{author}",
        )
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/api/friends/{request_id}/decline", response_class=JSONResponse)
async def api_friends_decline(
    request_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    ok = await decline_request(request_id, int(session["user_id"]))
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/api/friends/{request_id}/cancel", response_class=JSONResponse)
async def api_friends_cancel(
    request_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    ok = await cancel_request(request_id, int(session["user_id"]))
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/api/friends/{friend_id}/remove", response_class=JSONResponse)
async def api_friends_remove(
    friend_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Удалить из друзей. Переписка остаётся в истории, но писать нельзя."""
    ok = await unfriend(int(session["user_id"]), friend_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/api/friends/discoverable", response_model=None)
async def api_friends_discoverable(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse | RedirectResponse:
    """Тумблер «меня можно найти по поиску».

    Выключенный флаг делает человека ненаходимым ЛЮБЫМ способом — даже по
    точному e-mail и даже по прямому ``to_user_id`` в заявке.
    """
    uid = int(session["user_id"])
    value = bool(payload.get("value"))
    await set_discoverable(uid, value)
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"ok": True, "discoverable": value})
    return RedirectResponse("/friends", status_code=303)


__all__ = ["router"]
