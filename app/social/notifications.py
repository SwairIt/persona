"""Уведомления социального слоя: браузер / почта / Telegram.

Весь SQL темы живёт здесь (роуты его не видят — архитектурный гейт).

Модель
------
Событий четыре: заявка в друзья пришла, заявку приняли, новое личное
сообщение и «твой ИИ ответил за тебя». Каналов три, и каждый включается
ОТДЕЛЬНО для каждого события: «письма только про заявки, браузер про
всё» — нормальная и выразимая конфигурация.

Дефолты: браузер ВКЛ, почта и Telegram ВЫКЛ. Отсутствие строки в
``social_notif_pref`` и есть дефолт — новому человеку ничего не
бэкфиллится, а «почта выключена» никогда не зависит от того, успел ли
кто-то создать ему строки.

Изоляция
--------
Каждая функция принимает ``user_id`` получателя и пишет/читает ТОЛЬКО
его строки. Токен Telegram и chat id берутся из ``user_settings`` этого
же человека — то есть чужой токен структурно недостижим: нет запроса,
который читал бы конфиг одного пользователя при отправке другому.

Токен НИКОГДА не попадает в логи: он не логируется ни при успехе, ни
при ошибке (``TelegramAPIError`` в app/integrations/telegram/api.py
специально сконструирована так, чтобы не содержать URL с токеном).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction
from app.storage.repository import get_user_kv, set_user_kv

log = get_logger("persona.social.notifications")

Event = Literal["friend_request", "friend_accepted", "dm_message", "ai_replied"]
Channel = Literal["browser", "email", "telegram"]

EVENTS: tuple[Event, ...] = (
    "friend_request",
    "friend_accepted",
    "dm_message",
    "ai_replied",
)
CHANNELS: tuple[Channel, ...] = ("browser", "email", "telegram")

#: Дефолт по каналам. Браузер — единственный, что включён: он ничего не
#: отправляет наружу и не может «утечь» дальше вкладки. Почта и Telegram
#: выходят за пределы инстанса, поэтому только по явному включению.
_CHANNEL_DEFAULT: dict[Channel, bool] = {
    "browser": True,
    "email": False,
    "telegram": False,
}

#: Не чаще одного письма на ОДНУ переписку раз в 10 минут. Живая беседа —
#: это десятки сообщений в час; без окна почтовый ящик превращается в лог.
EMAIL_COOLDOWN_SECONDS = 600

#: Ключи Telegram-конфига в ``user_settings`` (изоляция — PK(user_id, key)).
TG_TOKEN_KEY = "social_tg_token"  # noqa: S105 — имя КЛЮЧА настройки, не сам токен
TG_CHAT_KEY = "social_tg_chat_id"

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


def _parse(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


# ── Настройки каналов ───────────────────────────────────────────────────────


Prefs = dict[str, dict[str, bool]]


def default_prefs() -> Prefs:
    return {
        event: {channel: _CHANNEL_DEFAULT[channel] for channel in CHANNELS}
        for event in EVENTS
    }


async def get_prefs(user_id: int) -> Prefs:
    """Полная матрица событие×канал ЭТОГО человека (дефолты + его строки)."""
    prefs = default_prefs()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT event, channel, enabled FROM social_notif_pref WHERE user_id = ?",
            (int(user_id),),
        )
        rows = await cursor.fetchall()
    for row in rows:
        event, channel = str(row["event"]), str(row["channel"])
        if event in prefs and channel in prefs[event]:
            prefs[event][channel] = int(row["enabled"] or 0) == 1
    return prefs


async def set_prefs(user_id: int, prefs: Prefs, now: datetime | None = None) -> None:
    """Переписать матрицу целиком. Неизвестные события/каналы игнорируем.

    Пишем ВСЕ комбинации явно (в т.ч. выключенные), а не «только
    включённые с удалением остального»: явная строка ``enabled=0``
    отличает «я выключил» от «я никогда не заходил», и смена дефолта в
    будущем не переиграет чужой осознанный выбор задним числом.
    """
    stamp = _fmt(now or utcnow())
    uid = int(user_id)
    payload: list[tuple[Any, ...]] = []
    for event in EVENTS:
        row = prefs.get(event) or {}
        for channel in CHANNELS:
            enabled = 1 if bool(row.get(channel)) else 0
            payload.append((uid, event, channel, enabled, stamp))
    async with write_transaction() as conn:
        await conn.executemany(
            """
            INSERT INTO social_notif_pref (user_id, event, channel, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, event, channel) DO UPDATE SET
                enabled    = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            payload,
        )


# ── Telegram: СВОЙ бот каждого человека ─────────────────────────────────────


class TelegramConfig(TypedDict):
    configured: bool
    chat_id: str
    token_tail: str


async def get_telegram_config(user_id: int) -> TelegramConfig:
    """Состояние привязки Telegram БЕЗ выдачи токена наружу.

    Возвращаем только «настроено или нет», chat id и последние 4 символа
    токена (чтобы человек узнал свой бот и понял, что строка не пустая).
    Полный токен не отдаётся ни одним роутом — записать можно, прочитать
    нельзя.
    """
    async with get_connection() as conn:
        token = await get_user_kv(conn, int(user_id), TG_TOKEN_KEY) or ""
        chat_id = await get_user_kv(conn, int(user_id), TG_CHAT_KEY) or ""
    token = str(token).strip()
    chat_id = str(chat_id).strip()
    return {
        "configured": bool(token and chat_id),
        "chat_id": chat_id,
        "token_tail": token[-4:] if len(token) >= 4 else "",
    }


async def set_telegram_config(user_id: int, token: str, chat_id: str) -> None:
    """Сохранить СВОЙ бот-токен и chat id. Пустой токен = отвязать.

    Токен здесь не логируется и никуда, кроме строки ``user_settings``
    этого пользователя, не уезжает.
    """
    clean_token = (token or "").strip()
    clean_chat = (chat_id or "").strip()
    async with get_connection() as conn:
        await set_user_kv(conn, int(user_id), TG_TOKEN_KEY, clean_token)
        await set_user_kv(conn, int(user_id), TG_CHAT_KEY, clean_chat)
    log.info(
        "social.notif.telegram_configured",
        user_id=int(user_id),
        configured=bool(clean_token and clean_chat),
    )


async def set_chat_id(user_id: int, chat_id: str) -> None:
    """Сменить только chat id, не трогая токен (его наружу не читаем)."""
    async with get_connection() as conn:
        await set_user_kv(conn, int(user_id), TG_CHAT_KEY, (chat_id or "").strip())


async def _telegram_credentials(user_id: int) -> tuple[str, str]:
    async with get_connection() as conn:
        token = await get_user_kv(conn, int(user_id), TG_TOKEN_KEY) or ""
        chat_id = await get_user_kv(conn, int(user_id), TG_CHAT_KEY) or ""
    return str(token).strip(), str(chat_id).strip()


async def send_telegram(user_id: int, text: str) -> str:
    """Отправить сообщение в личный бот ЭТОГО человека. Никогда не бросает.

    Возвращает статус-строку (``sent`` / ``not_configured`` / ``failed``)
    — вызывающий фон только логирует её.
    """
    token, chat_id = await _telegram_credentials(user_id)
    if not token or not chat_id:
        return "not_configured"
    try:
        chat = int(chat_id)
    except (TypeError, ValueError):
        return "bad_chat_id"
    try:
        from app.integrations.telegram.api import TelegramBotAPI  # noqa: PLC0415

        await TelegramBotAPI(token).send_message(chat, text)
    except Exception as exc:  # noqa: BLE001 — уведомление не должно ронять ход
        # str(exc) у TelegramAPIError гарантированно без URL с токеном.
        log.warning(
            "social.notif.telegram_failed", user_id=int(user_id), error=str(exc)[:200]
        )
        return "failed"
    return "sent"


# ── Очередь для браузера ────────────────────────────────────────────────────


class NotifItem(TypedDict):
    id: int
    event: str
    title: str
    body: str
    url: str
    created_at: str


async def queue_browser(
    user_id: int, event: str, title: str, body: str, url: str = ""
) -> int:
    """Положить браузерное уведомление в очередь ПОЛУЧАТЕЛЯ.

    ``body`` — это выдержка из личного сообщения (до 300 символов). Хранить её
    открытым текстом рядом с зашифрованной перепиской бессмысленно: утечка
    цитаты — это утечка переписки. Поэтому шифруем ключом ПОЛУЧАТЕЛЯ (строка
    принадлежит ему, читает её тоже только он). Заголовок остаётся открытым:
    это «Сообщение от <имя>», без содержимого.
    """
    from app.member_crypto import encrypt_for_user  # noqa: PLC0415 — цикл импорта

    async with write_transaction() as conn:
        stored_body = await encrypt_for_user(int(user_id), body[:500], conn)
        cursor = await conn.execute(
            "INSERT INTO social_notif_item (user_id, event, title, body, url) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(user_id), str(event), title[:200], stored_body, url[:300]),
        )
        return int(cursor.lastrowid or 0)


async def take_pending(user_id: int, limit: int = 20) -> list[NotifItem]:
    """Забрать ещё не показанные уведомления ЭТОГО человека и пометить их.

    Читаем и помечаем в ОДНОЙ транзакции: две открытые вкладки не должны
    показать одно и то же уведомление дважды.
    """
    uid = int(user_id)
    safe_limit = max(1, min(int(limit), 50))
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "SELECT id, event, title, body, url, created_at FROM social_notif_item "
            "WHERE user_id = ? AND delivered_at IS NULL ORDER BY id ASC LIMIT ?",
            (uid, safe_limit),
        )
        rows = list(await cursor.fetchall())
        if rows:
            placeholders = ",".join("?" for _ in rows)
            await conn.execute(
                "UPDATE social_notif_item SET delivered_at = datetime('now') "  # noqa: S608
                f"WHERE user_id = ? AND id IN ({placeholders})",
                [uid, *[int(r["id"]) for r in rows]],
            )
    from app.member_crypto import decrypt_for_user  # noqa: PLC0415 — цикл импорта

    return [
        {
            "id": int(row["id"]),
            "event": str(row["event"]),
            "title": str(row["title"]),
            "body": await decrypt_for_user(uid, row["body"]),
            "url": str(row["url"] or ""),
            "created_at": str(row["created_at"] or ""),
        }
        for row in rows
    ]


# ── Антиспам почты ──────────────────────────────────────────────────────────


async def email_allowed(user_id: int, scope: str, now: datetime) -> bool:
    """Прошло ли окно с прошлого письма по ЭТОМУ поводу (см. scope)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT last_sent_at FROM social_notif_cooldown "
            "WHERE user_id = ? AND scope = ?",
            (int(user_id), str(scope)),
        )
        row = await cursor.fetchone()
    if row is None:
        return True
    last = _parse(str(row["last_sent_at"] or ""))
    if last is None:
        return True
    return (now.astimezone(UTC) - last).total_seconds() >= EMAIL_COOLDOWN_SECONDS


async def mark_email_sent(user_id: int, scope: str, now: datetime) -> None:
    async with write_transaction() as conn:
        await conn.execute(
            """
            INSERT INTO social_notif_cooldown (user_id, scope, last_sent_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, scope) DO UPDATE SET last_sent_at = excluded.last_sent_at
            """,
            (int(user_id), str(scope), _fmt(now)),
        )


async def _user_email(user_id: int) -> str:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT email FROM users WHERE id = ?", (int(user_id),)
        )
        row = await cursor.fetchone()
    return str(row["email"]) if row is not None else ""


# ── Фасад: одно событие → все включённые каналы ─────────────────────────────


async def notify(
    user_id: int,
    event: Event,
    *,
    title: str,
    body: str = "",
    url: str = "",
    scope: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    """Разослать одно событие по КАНАЛАМ ЭТОГО человека. Никогда не бросает.

    ``scope`` — ключ антиспама почты (например ``dm:17``). Пустой →
    используем сам тип события: два письма «тебе пришла заявка» подряд
    тоже незачем.

    Возвращает статус по каждому каналу (для тестов и отладки).
    """
    uid = int(user_id)
    moment = now or utcnow()
    result: dict[str, str] = {}
    try:
        prefs = await get_prefs(uid)
    except Exception as exc:  # noqa: BLE001
        log.warning("social.notif.prefs_failed", user_id=uid, error=str(exc))
        return {"error": "prefs_failed"}
    channels = prefs.get(event, {})

    if channels.get("browser"):
        try:
            await queue_browser(uid, event, title, body, url)
            result["browser"] = "queued"
        except Exception as exc:  # noqa: BLE001
            log.warning("social.notif.browser_failed", user_id=uid, error=str(exc))
            result["browser"] = "failed"

    if channels.get("email"):
        key = scope or f"event:{event}"
        try:
            if not await email_allowed(uid, key, moment):
                result["email"] = "cooldown"
            else:
                address = await _user_email(uid)
                if not address:
                    result["email"] = "no_address"
                else:
                    from app.smtp_delivery import send_email  # noqa: PLC0415

                    status = await send_email(address, title, body or title)
                    result["email"] = str(status.get("status") or "unknown")
                    # Окно взводим ДАЖЕ на неуспехе: иначе лежащий SMTP
                    # превращал бы каждое сообщение в новую попытку
                    # соединения — то есть в тот же спам, но по сети.
                    await mark_email_sent(uid, key, moment)
        except Exception as exc:  # noqa: BLE001
            log.warning("social.notif.email_failed", user_id=uid, error=str(exc))
            result["email"] = "failed"

    if channels.get("telegram"):
        text = f"{title}\n{body}".strip()
        result["telegram"] = await send_telegram(uid, text)

    return result


async def _user_name(user_id: int) -> str:
    """Отображаемое имя (та же маска e-mail, что и во всём социальном UI)."""
    from app.social.repository import _card_name  # noqa: PLC0415 — общий пакет

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT email, display_name FROM users WHERE id = ?", (int(user_id),)
        )
        row = await cursor.fetchone()
    if row is None:
        return "кто-то"
    return _card_name(row["display_name"], str(row["email"]))


async def notify_friend_request(
    to_user_id: int, from_user_id: int, message: str = "", now: datetime | None = None
) -> dict[str, str]:
    """«Тебе пришла заявка в друзья от X»."""
    name = await _user_name(from_user_id)
    return await notify(
        int(to_user_id),
        "friend_request",
        title=f"Заявка в друзья: {name}",
        body=(message or "").strip()[:300] or "Хочет добавить тебя в друзья.",
        url="/friends",
        scope="event:friend_request",
        now=now,
    )


async def notify_friend_accepted(
    to_user_id: int, by_user_id: int, now: datetime | None = None
) -> dict[str, str]:
    """«X принял твою заявку»."""
    name = await _user_name(by_user_id)
    return await notify(
        int(to_user_id),
        "friend_accepted",
        title=f"{name} принял твою заявку",
        body="Теперь вы друзья — можно написать.",
        url="/friends",
        scope="event:friend_accepted",
        now=now,
    )


__all__ = [
    "CHANNELS",
    "EMAIL_COOLDOWN_SECONDS",
    "EVENTS",
    "TG_CHAT_KEY",
    "TG_TOKEN_KEY",
    "Channel",
    "Event",
    "NotifItem",
    "Prefs",
    "TelegramConfig",
    "default_prefs",
    "email_allowed",
    "get_prefs",
    "get_telegram_config",
    "mark_email_sent",
    "notify",
    "notify_friend_accepted",
    "notify_friend_request",
    "queue_browser",
    "send_telegram",
    "set_chat_id",
    "set_prefs",
    "set_telegram_config",
    "take_pending",
]
