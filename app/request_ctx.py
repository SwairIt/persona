"""Per-request identity for SYNCHRONOUS readers (Jinja globals, i18n).

Почему отдельный модуль, а не поле в ``request.state``
------------------------------------------------------
Jinja-глобалы (:func:`app.web.templates_engine.get_theme`) и резолвер языка
(:func:`app.i18n.get_ui_language`) синхронны и вызываются БЕЗ доступа к
``Request``: шаблон зовёт ``{{ get_theme() }}`` из ``base.html``, а ``t()``
дёргается сотни раз за рендер. Пробросить туда ``request`` можно было бы
только правкой ~300 роутов, поэтому личность участника кладём в
:class:`~contextvars.ContextVar` — auth-гейт выставляет её на входе и
сбрасывает в ``finally`` после ответа.

Контракт значения
-----------------
``None`` — анонимный запрос ИЛИ владелец (обе группы читают ГЛОБАЛЬНЫЙ
``kv_settings``, поведение 1:1 со старым кодом). ``int`` — id участника
(зарегистрированный не-владелец), его настройки живут в ``user_settings``.

Модуль намеренно НИ ОТ ЧЕГО не зависит: его импортируют и ``app.i18n``, и
``app.web.templates_engine`` (который сам импортирует i18n), и middleware —
любая зависимость здесь мгновенно создала бы цикл.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

# id участника (не-владельца) текущего запроса; None = аноним или владелец.
current_member_uid: ContextVar[int | None] = ContextVar(
    "persona_current_member_uid", default=None
)


def set_member_uid(uid: int | None) -> Token[int | None]:
    """Выставить личность участника на текущий запрос, вернуть токен сброса."""
    return current_member_uid.set(uid)


def reset_member_uid(token: Token[int | None]) -> None:
    """Сбросить личность (вызывать в ``finally`` после отдачи ответа)."""
    current_member_uid.reset(token)


def get_member_uid() -> int | None:
    """id участника или ``None`` (аноним/владелец → глобальные настройки)."""
    return current_member_uid.get()


__all__ = [
    "current_member_uid",
    "get_member_uid",
    "reset_member_uid",
    "set_member_uid",
]
