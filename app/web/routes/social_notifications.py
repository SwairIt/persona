"""Уведомления социального слоя — /settings/notifications-social.

Страница участника (и владельца): матрица «событие × канал», привязка
СВОЕГО Telegram-бота и список переписок, где включён ИИ, с рубильником
«выключить везде».

Весь SQL — в :mod:`app.social.notifications` и :mod:`app.social.ai_pref`
(архитектурный гейт запрещает роутам прямой доступ к БД).

Почему СВОЙ бот, а не общий
---------------------------
У владельца инстанса уже есть бот (``app/integrations/telegram``), и
соблазн «пусть он пишет всем» велик. Мы этого НЕ делаем, по трём
причинам, каждой из которых хватило бы:

* чтобы узнать chat id участника, пришлось бы читать входящие ОБЩЕГО
  бота — то есть строить механизм, который по построению видит чужую
  переписку с ботом владельца;
* ``getUpdates`` — однопотребительский: второй читатель очереди крадёт
  апдейты у воркера владельца, и его ассистент начинает терять сообщения;
* уведомления участника оказались бы завязаны на то, запущен ли у
  владельца бот, и уходили бы через ЕГО токен — то есть от его имени.

Поэтому участник заводит собственного бота у @BotFather и вписывает свои
токен и chat id. Токен хранится в ЕГО ``user_settings`` (PK(user_id, key)
— чужой недостижим), наружу никогда не отдаётся (только 4 последних
символа) и не попадает в логи.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.social import ai_pref, notifications
from app.web.templates_engine import templates

router = APIRouter(tags=["settings", "social"])
log = get_logger("persona.social.notif_settings")

_PAGE = "/settings/notifications-social"

# Порядок и подписи строк матрицы. Ключи переводов — в app/translations/*.
_EVENT_ROWS: tuple[tuple[str, str], ...] = (
    ("friend_request", "social_notif_event_friend_request"),
    ("friend_accepted", "social_notif_event_friend_accepted"),
    ("dm_message", "social_notif_event_dm_message"),
    ("ai_replied", "social_notif_event_ai_replied"),
)
_CHANNEL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("browser", "social_notif_channel_browser"),
    ("email", "social_notif_channel_email"),
    ("telegram", "social_notif_channel_telegram"),
)


@router.get("/settings/notifications-social", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    saved: int = 0,
    tg: str = "",
) -> HTMLResponse:
    uid = int(session["user_id"])
    return templates.TemplateResponse(
        request,
        "settings_notifications_social.html",
        {
            "title": "Уведомления",
            "active_nav": "settings",
            "prefs": await notifications.get_prefs(uid),
            "events": _EVENT_ROWS,
            "channels": _CHANNEL_COLUMNS,
            "telegram": await notifications.get_telegram_config(uid),
            "ai_threads": await ai_pref.list_active(uid),
            "email_cooldown_minutes": notifications.EMAIL_COOLDOWN_SECONDS // 60,
            "saved": bool(saved),
            "tg_status": tg,
        },
    )


@router.post("/settings/notifications-social", response_model=None)
async def notifications_save(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Сохранить матрицу каналов и (опционально) Telegram-привязку.

    Одна форма и один POST: «галочки» и «мой бот» — это одна настройка
    уведомлений, и раздельные кнопки только заставляли бы человека
    сохранять дважды.

    Пустое поле токена НЕ стирает уже сохранённый токен (иначе любое
    сохранение галочек отвязывало бы Telegram — полный токен в форму мы
    не подставляем принципиально). Чтобы отвязать, есть отдельная
    галочка ``tg_clear``.
    """
    uid = int(session["user_id"])
    form = await request.form()

    prefs: notifications.Prefs = {}
    for event in notifications.EVENTS:
        prefs[event] = {
            channel: form.get(f"{event}__{channel}") is not None
            for channel in notifications.CHANNELS
        }
    await notifications.set_prefs(uid, prefs)

    token = str(form.get("tg_token") or "").strip()
    chat_id = str(form.get("tg_chat_id") or "").strip()
    if form.get("tg_clear") is not None:
        await notifications.set_telegram_config(uid, "", "")
    elif token:
        await notifications.set_telegram_config(uid, token, chat_id)
    elif chat_id:
        current = await notifications.get_telegram_config(uid)
        if current["configured"] and chat_id != current["chat_id"]:
            # Меняем ТОЛЬКО chat id: полный токен наружу не читается вовсе,
            # поэтому «сохранить заново» его бы стёрло.
            await notifications.set_chat_id(uid, chat_id)

    return RedirectResponse(f"{_PAGE}?saved=1", status_code=303)


@router.post("/api/social-notif/telegram/test", response_class=JSONResponse)
async def telegram_test(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Отправить пробное сообщение в СВОЙ бот — проверить привязку.

    Пользуется исключительно конфигом вызывающего: чужой токен сюда
    попасть не может, потому что он читается по его же ``user_id``.
    """
    uid = int(session["user_id"])
    status = await notifications.send_telegram(
        uid, "Persona: уведомления подключены ✅"
    )
    return JSONResponse({"status": status})


@router.get("/api/social-notif/pending", response_class=JSONResponse)
async def pending(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Очередь браузерных уведомлений ЭТОГО человека (и пометка «показано»).

    Отдельного поллера страница не заводит: этот эндпоинт дренирует уже
    существующий таймер ``unreadStore`` в ``base.html`` (тот, что раз в
    минуту обновляет бейдж непрочитанного).
    """
    uid = int(session["user_id"])
    items: list[dict[str, Any]] = [
        dict(item) for item in await notifications.take_pending(uid)
    ]
    return JSONResponse({"notifications": items})


__all__ = ["router"]
