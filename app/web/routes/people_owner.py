"""``/root/people`` — люди и рост. Владельцу: кто пришёл, сколько, откуда куда.

Почему отдельная страница, а не секция в ``/root/analytics``
-----------------------------------------------------------
Владелец сформулировал запрос прямо: «не хочу видеть их переписку, хочу видеть,
кто зарегистрировался и как идёт рост». Это ДРУГОЙ вопрос, чем «что происходит
на сайте», и у него другой источник: ``users.created_at`` работает за всю
историю инстанса, а события счётчика — только с даты его включения. Дашборд
аналитики честно рисует рамку «данные с такого-то числа»; помесячный график
регистраций за год внутри этой рамки читался бы как обрезанный, хотя он полон.

Ещё одна причина — приватность. На этой странице действует правило, которого
на дашборде нет: ЧУЖОЙ ТЕКСТ СЮДА НЕ ПОПАДАЕТ НИКОГДА. Держать такое правило
в куске большой страницы, где рядом крутятся клики, пути и метки элементов,
означает потерять его при первой же правке соседнего блока.

Гейт: два рубежа, а не один
---------------------------
1. ``_OWNER_ONLY_PREFIXES = ("/root",)`` в ``app/web/middleware/auth_gate.py``
   закрывает весь префикс.
2. Хендлер ЗАНОВО резолвит владельца — и именно fail-closed резолвером
   ``app.web.routes.owner_view.viewer_is_owner``: любой сбой резолва даёт
   «не владелец» → 403, а не 500 и не тихую выдачу. Гейт можно
   переконфигурировать (kv ``role_gate_enabled`` и соседние флаги — живые
   переключатели), эту строчку — нет.

────────────────────────────────────────────────────────────────────────────
ПРАВИЛО, КОТОРОЕ НЕЛЬЗЯ НАРУШИТЬ ПРАВКОЙ ЭТОГО ФАЙЛА
────────────────────────────────────────────────────────────────────────────
Этот роут НЕ ХОДИТ В БАЗУ САМ. У него нет ни ``get_connection``, ни строки
SQL — всё, что он показывает, приходит готовым из
:func:`app.analytics.people.build_people_view` в виде объектов
:class:`app.analytics.people.Person` (``slots=True``, одиннадцать полей: id,
адрес, роль, статус, даты, счётчики, флаги). Свободного текста в этих объектах
нет ни одного поля, поэтому в шаблон нечего передать, кроме метаданных.

Практическое следствие: чтобы показать здесь чужое сообщение, недостаточно
дописать строчку в шаблон — придётся сначала добавить поле в ``Person`` и
провести туда текст через ``people.py``, где на запрещённые таблицы стоит
тест по исходнику (``tests/test_owner_people_view.py``). Это и есть
«структурно недоступно»: не запрет в комментарии, а отсутствие канала.

Действия над аккаунтом (заморозить / вернуть / удалить) сознательно НЕ имеют
здесь собственных эндпоинтов: они отправляются в уже существующий
``POST /root/users/{uid}/{op}`` (``app/web/routes/root_control.py``) с полем
``next``. Второй набор ручек означал бы два места, где решается «можно ли
снести этот аккаунт», и гарантированное расхождение гардов.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.web.routes.owner_view import viewer_is_owner as is_owner
from app.web.templates_engine import templates

router = APIRouter(tags=["root"])
log = get_logger("persona.people")


@router.get("/root/people", response_class=HTMLResponse)
async def people_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Список аккаунтов + прирост + судьба прошлой когорты.

    Ошибка сборки не отдаёт 500: страница остаётся живой с текстом ошибки —
    как на дашборде аналитики. Пульт владельца не имеет права падать из-за
    отчёта, который он показывает.
    """
    uid = session["user_id"]
    if not await is_owner(uid):
        # 403 и НИ ОДНОГО байта данных: сборка вьюмодели даже не начинается.
        raise HTTPException(status_code=403, detail="только для владельца")

    from app.analytics import people  # noqa: PLC0415 — тяжёлый модуль, не на импорте

    try:
        data: dict[str, Any] = await people.build_people_view(owner_id=uid)
        error = ""
    except Exception as exc:  # noqa: BLE001 — пустая страница лучше 500
        # В журнал уходит текст ошибки и НИ ОДНОГО адреса: e-mail показывается
        # владельцу на экране и больше нигде (см. шапку app/analytics/people.py).
        log.warning("people.view_failed", error=str(exc))
        data, error = {}, str(exc)

    return templates.TemplateResponse(
        request,
        "root_people.html",
        {
            "title": "Люди и рост — пульт владельца",
            "active_nav": "root",
            "is_owner": True,
            "p": data,
            "error": error,
        },
    )


__all__ = ["router"]
