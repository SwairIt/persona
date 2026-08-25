"""Весь SQL поддержки: обращения, ответы владельца, инстансная соль.

Роуты сюда ходят через функции этого модуля и НЕ видят ни одной строки SQL —
этого требует архитектурный гейт (``tests/test_architecture_gates.py``:
новый модуль в ``app/web/routes/`` не имеет права импортировать
``get_connection`` / ``write_transaction``).

Изоляция
--------
Обращения в поддержку — инстанс-глобальные данные ВЛАДЕЛЬЦА: он их читает,
он на них отвечает. Ни одна функция чтения тут не вызывается из member-зоны,
и ни одна не принимает «покажи обращения пользователя X» из запроса —
:func:`list_tickets` фильтрует только по статусу. Проверку «это владелец»
делают роуты (``_require_owner``) поверх гейта: гейт закрывает ``/settings/*``
не-владельцу, но полагаться на один рубеж в ящике с чужими email нельзя.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction

log = get_logger("persona.support")

#: kv-ключ инстансной соли. Одно значение на инстанс, генерируется при первом
#: обращении к нему и БОЛЬШЕ НЕ МЕНЯЕТСЯ: смена соли обнулила бы связность
#: старых ``ip_hash`` между собой (а восстановить её из хэшей нельзя).
_SALT_KEY: Final[str] = "support_ip_salt"

#: Процесс-кэш соли. Читается на каждой отправке формы и на каждом рендере
#: страницы (подпись времени) — ходить за ней в SQLite каждый раз незачем,
#: значение неизменяемо по построению.
_salt_cache: dict[str, bytes | None] = {"value": None}

#: Статусы обращения. Порядок = порядок вкладок фильтра у владельца.
STATUSES: Final[tuple[str, ...]] = ("new", "read", "answered", "closed")


def reset_salt_cache() -> None:
    """Сбросить процесс-кэш соли (тесты: у каждого своя временная БД)."""
    _salt_cache["value"] = None


async def instance_salt() -> bytes:
    """Инстансная соль (32 байта). Создаётся один раз, потом только читается.

    Гонка двух воркеров разрешается на уровне БД: ``INSERT OR IGNORE`` +
    повторное чтение. Проигравший берёт чужое значение, а не перезаписывает
    его своим — иначе первый же параллельный старт разошёлся бы солями и
    ``ip_hash`` одного и того же адреса перестали бы совпадать.
    """
    cached = _salt_cache["value"]
    if cached is not None:
        return cached
    fresh = secrets.token_hex(32)
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO kv_settings (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (_SALT_KEY, fresh),
        )
        await conn.commit()
        cursor = await conn.execute(
            "SELECT value FROM kv_settings WHERE key = ?", (_SALT_KEY,)
        )
        row = await cursor.fetchone()
    value = str(row["value"]) if row is not None else fresh
    salt = value.encode("utf-8")
    _salt_cache["value"] = salt
    return salt


async def hash_ip(ip: str | None) -> str:
    """``sha256(соль || "ip:" || ip)``, первые 16 hex. Пустой вход → ``""``.

    Ровно один вопрос, на который это отвечает: «два обращения пришли из
    одного места или из двух?». Обратного пути нет — сырой адрес не
    сохраняется нигде, а без инстансной соли всё пространство IPv4
    перебирается за секунды, и «хэш» был бы синонимом адреса.
    """
    text = (ip or "").strip()
    if not text:
        return ""
    salt = await instance_salt()
    return hmac.new(salt, b"ip:" + text.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


# ── Подпись формы (минимальное время на форме) ──────────────────────────────


async def sign_form_ts(now: float | None = None) -> str:
    """Значение скрытого поля ``ts``: ``"<unixtime>.<подпись>"``.

    Подпись нужна, потому что без неё поле — это просто число, которое бот
    подставляет любым. HMAC на ИНСТАНСНОЙ соли (а не на процессной): под
    ``uvicorn --workers 3`` форму рисует один воркер, а принимает другой, и
    процессный секрет ломал бы отправку у каждого третьего человека.
    """
    stamp = int(now if now is not None else time.time())
    salt = await instance_salt()
    sig = hmac.new(
        salt, b"form:" + str(stamp).encode("ascii"), hashlib.sha256
    ).hexdigest()[:32]
    return f"{stamp}.{sig}"


async def verify_form_ts(raw: str | None, now: float | None = None) -> float | None:
    """Сколько секунд человек провёл на форме, или ``None``.

    ``None`` = подписи нет, она не сходится или протухла. Отрицательный
    результат (часы сервера ушли назад) тоже ``None``: «отправлено раньше,
    чем выдана форма» — не ответ, а сломанное состояние.
    """
    text = (raw or "").strip()
    stamp_text, _, sig = text.partition(".")
    if not stamp_text.isdigit() or not sig:
        return None
    salt = await instance_salt()
    expected = hmac.new(
        salt, b"form:" + stamp_text.encode("ascii"), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    elapsed = (now if now is not None else time.time()) - int(stamp_text)
    if elapsed < 0:
        return None
    from app.support.service import MAX_SECONDS_ON_FORM  # noqa: PLC0415 — цикл

    if elapsed > MAX_SECONDS_ON_FORM:
        return None
    return elapsed


# ── Обращения ───────────────────────────────────────────────────────────────


async def create_ticket(
    *,
    user_id: int | None,
    email: str,
    subject: str,
    body: str,
    role: str,
    source_page: str,
    app_version: str,
    browser_class: str,
    ip_hash: str,
) -> int:
    """Записать обращение. Возвращает id.

    Это ЕДИНСТВЕННОЕ действие, которое обязано случиться при отправке формы:
    письмо владельцу — уже best-effort сверху (см. :mod:`app.support.notify`).
    Порядок именно такой — сначала строка, потом попытка письма, — чтобы
    обращение не могло потеряться из-за почты.
    """
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO support_ticket "
            "(user_id, email, subject, body, role, source_page, app_version, "
            " browser_class, ip_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(user_id) if user_id is not None else None,
                email,
                subject,
                body,
                role,
                source_page,
                app_version,
                browser_class,
                ip_hash,
            ),
        )
        return int(cursor.lastrowid or 0)


async def mark_owner_notified(ticket_id: int, status: str) -> None:
    """Записать ЧЕСТНЫЙ исход попытки уведомить владельца письмом.

    ``status`` — либо ``'sent'``, либо ``'skipped:<причина>'`` /
    ``'error:<причина>'``. Значение показывается владельцу дословно: он должен
    видеть, что письма НЕ БЫЛО, а не догадываться об этом.
    """
    async with write_transaction() as conn:
        await conn.execute(
            "UPDATE support_ticket SET owner_notify_status = ?, "
            "owner_notified_at = datetime('now') WHERE id = ?",
            (status[:120], int(ticket_id)),
        )


async def list_tickets(
    status: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Лента обращений, свежие сверху. ``status=None`` — все."""
    sql = "SELECT * FROM support_ticket"
    params: list[Any] = []
    if status in STATUSES:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    async with get_connection() as conn:
        cursor = await conn.execute(sql, tuple(params))
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_ticket(ticket_id: int) -> dict[str, Any] | None:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM support_ticket WHERE id = ?", (int(ticket_id),)
        )
        row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def status_counts() -> dict[str, int]:
    """Счётчики по статусам + ``total``. Основа фильтра и бейджа."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT status, COUNT(*) AS n FROM support_ticket GROUP BY status"
        )
        rows = await cursor.fetchall()
    counts = {name: 0 for name in STATUSES}
    for row in rows:
        name = str(row["status"])
        if name in counts:
            counts[name] = int(row["n"] or 0)
    counts["total"] = sum(counts[name] for name in STATUSES)
    return counts


async def set_status(ticket_id: int, status: str) -> bool:
    """Перевести обращение в ``status``. ``False`` — статус неизвестен/нет строки.

    Список разрешённых значений проверяется ЗДЕСЬ, а не только CHECK'ом в
    схеме: CHECK превратил бы опечатку в 500 у владельца посреди ящика, а
    здесь она честно возвращает «не сделано».
    """
    if status not in STATUSES:
        return False
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "UPDATE support_ticket SET status = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (status, int(ticket_id)),
        )
        return int(cursor.rowcount or 0) > 0


async def delete_ticket(ticket_id: int) -> bool:
    """Удалить обращение вместе с перепиской по нему. ``False`` — строки не было.

    Существует ради обещания под формой: «хранится, пока владелец не удалит».
    Обещание без кнопки — это не политика хранения, а текст. Сообщения уходят
    каскадом (``ON DELETE CASCADE`` в схеме), поэтому отдельного DELETE по
    ``support_message`` тут нет и не должно появиться: два места удаления
    рано или поздно разъезжаются, и остаются осиротевшие ответы с email.
    """
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "DELETE FROM support_ticket WHERE id = ?", (int(ticket_id),)
        )
        return int(cursor.rowcount or 0) > 0


# ── Ответы ──────────────────────────────────────────────────────────────────


async def add_message(
    ticket_id: int,
    body: str,
    *,
    author: str = "owner",
    delivery_status: str = "pending",
) -> int:
    """Сохранить ответ. Текст записывается ДО попытки отправки письма.

    Именно в таком порядке: если почта упадёт (а она на этом инстансе
    сломана), написанный владельцем ответ обязан остаться в базе. Исход
    доставки дописывается следом через :func:`set_message_delivery`.
    """
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO support_message (ticket_id, author, body, delivery_status) "
            "VALUES (?, ?, ?, ?)",
            (int(ticket_id), author, body, delivery_status[:120]),
        )
        return int(cursor.lastrowid or 0)


async def set_message_delivery(message_id: int, delivery_status: str) -> None:
    async with write_transaction() as conn:
        await conn.execute(
            "UPDATE support_message SET delivery_status = ? WHERE id = ?",
            (delivery_status[:120], int(message_id)),
        )


async def list_messages(ticket_id: int) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM support_message WHERE ticket_id = ? ORDER BY id",
            (int(ticket_id),),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ── Адреса ──────────────────────────────────────────────────────────────────


async def owner_email() -> str:
    """Адрес, на который зарегистрирован владелец. ЧИТАЕТСЯ ИЗ БД, не хардкод.

    Владелец = kv ``owner_user_id``, иначе минимальный ``users.id`` — та же
    логика, что в :mod:`app.auth.owner`, но без её процесс-кэша: адрес нужен
    редко (одно письмо на обращение), а протухший кэш здесь означал бы письма
    на старый адрес после смены владельца.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'owner_user_id'"
        )
        row = await cursor.fetchone()
        raw = str(row["value"]).strip() if row is not None else ""
        if raw.isdigit():
            cursor = await conn.execute(
                "SELECT email FROM users WHERE id = ?", (int(raw),)
            )
        else:
            cursor = await conn.execute(
                "SELECT email FROM users ORDER BY id LIMIT 1"
            )
        row = await cursor.fetchone()
    return str(row["email"]) if row is not None and row["email"] else ""


async def user_email(user_id: int | None) -> str:
    """Актуальный адрес аккаунта (для ответа залогиненному автору)."""
    if user_id is None:
        return ""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT email FROM users WHERE id = ?", (int(user_id),)
        )
        row = await cursor.fetchone()
    return str(row["email"]) if row is not None and row["email"] else ""
