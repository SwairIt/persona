"""Захват просмотров страниц. Один буферный append на запрос — и всё.

Место в стеке
-------------
СТРОГО внутри :class:`~app.web.middleware.auth_gate.AuthGateMiddleware`: роль
зрителя берётся из ``request.state.is_owner`` / ``request.state.user_id``,
которые кладёт гейт (и кладёт теперь в том числе на публичных путях). Своего
резолва сессии здесь нет намеренно — лишний запрос в БД на каждый хит это
ровно та цена, которую аналитика не имеет права брать с сайта.

Что делает на пути ответа
-------------------------
Читает три заголовка, считает два HMAC и кладёт словарь в ``deque``. Никакого
ввода-вывода: в БД пачку сливает фоновая задача (см.
:mod:`app.analytics.capture`). Весь блок обёрнут в ``try/except``, который
глотает ВСЁ: счётчик посещений не имеет права уронить страницу, которую он
считает. Это проверяется тестом (``test_owner_analytics.py``:
«analytics failure never breaks the request»).

Согласие
--------
Решение и его обоснование целиком описаны в докстринге
:mod:`app.analytics.capture`. Коротко: обезличенный хит анонима пишется без
согласия (это счётчик, эквивалент строки access-лога, и он ничего не
связывает), а всё, по чему два визита склеиваются в одно посещение —
псевдоним сессии, «первый визит», реферер — только при ``persona_consent=all``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.analytics import capture
from app.auth import SESSION_COOKIE_NAME
from app.logging_setup import get_logger

log = get_logger("persona.analytics.middleware")

#: Окно, внутри которого повторный хит той же сессии считается НЕ первым.
#: Живёт в памяти воркера и умирает вместе с ним — поэтому «первый визит»
#: измерен приблизительно, и дашборд подписывает его именно так.
_SEEN_TTL = 1800.0
_SEEN_LIMIT = 20_000
_seen: dict[str, float] = {}


def reset_seen() -> None:
    """Забыть, какие сессии уже видели (тесты; перезапуск воркера)."""
    _seen.clear()


def _first_visit(session_hash: str | None) -> bool:
    if not session_hash:
        return False
    now = time.monotonic()
    if len(_seen) > _SEEN_LIMIT:
        # Дешёвая обрезка: словарь не должен расти без границы на инстансе,
        # который кто-то решил просканировать. Точность «первого визита» тут
        # приносится в жертву осознанно — это оценка, а не бухгалтерия.
        stale = [k for k, v in _seen.items() if now - v > _SEEN_TTL]
        for key in stale:
            _seen.pop(key, None)
        if len(_seen) > _SEEN_LIMIT:
            _seen.clear()
    last = _seen.get(session_hash)
    _seen[session_hash] = now
    return last is None or now - last > _SEEN_TTL


def _role(request: Request) -> tuple[str, int | None]:
    uid = getattr(request.state, "user_id", None)
    if uid is None:
        return capture.ROLE_ANONYMOUS, None
    if getattr(request.state, "is_owner", False):
        return capture.ROLE_OWNER, int(uid)
    return capture.ROLE_MEMBER, int(uid)


class AnalyticsMiddleware(BaseHTTPMiddleware):
    """Пишет один ``view`` на каждый GET страницы. Никогда не мешает ответу."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Any]
    ) -> Response:
        path = request.url.path
        interesting = request.method == "GET" and not capture.should_skip(path)
        if interesting:
            # Единственный await до ответа — и он попадает в кэш на 30 с,
            # то есть в БД ходит примерно раз в полминуты на воркер.
            with suppress(Exception):
                await capture.refresh_state()
        response = await call_next(request)
        if interesting and capture.is_enabled():
            try:
                self._record(request, response)
            except Exception as exc:  # noqa: BLE001 — счётчик не ломает страницу
                log.debug("analytics.capture_failed", error=str(exc))
        return response

    @staticmethod
    def _record(request: Request, response: Response) -> None:
        role, uid = _role(request)
        consented = (
            request.cookies.get(capture.CONSENT_COOKIE) == capture.CONSENT_GRANTED
        )
        device = capture.device_class(request.headers.get("user-agent", ""))
        salt = capture.current_salt()
        client = request.client
        session_hash = capture.session_pseudonym(
            salt=salt,
            session_token=request.cookies.get(SESSION_COOKIE_NAME),
            client_ip=client.host if client else None,
            device=device,
            day=time.strftime("%Y-%m-%d", time.gmtime()),
            consented=consented,
            authenticated=uid is not None,
        )
        # Реферер — только там, где визиты и так связываются: у вошедшего (это
        # его аккаунт, основание — договор) и у анонима, давшего согласие.
        referrer = ""
        if uid is not None or consented:
            referrer = capture.referrer_host(request.headers.get("referer", ""))
        capture.record(
            kind=capture.KIND_VIEW,
            path=capture.normalise_path(request.app, request.url.path),
            role=role,
            device=device,
            referrer=referrer,
            session_hash=session_hash,
            user_id=uid,
            first_view=_first_visit(session_hash),
            status=getattr(response, "status_code", None),
        )


__all__ = ["AnalyticsMiddleware", "reset_seen"]
