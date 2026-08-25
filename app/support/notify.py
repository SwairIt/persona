"""Почта поддержки — ЗЕРКАЛО сайта, а не канал доставки.

СОСТОЯНИЕ ЭТОГО ИНСТАНСА — ИЗМЕРЕНО, а не принято на слово. Формулировка
«``smtp_enabled='true'`` при пустых ``smtp_host``/``smtp_from``, значит
``misconfigured``» ОКАЗАЛАСЬ НЕВЕРНОЙ: :func:`app.smtp_delivery._load_settings`
при пустом kv добирает значения из ``.env`` (``PERSONA_SMTP_*``), а
``smtp_from`` доводит из ``smtp_user``. В репозитории такой ``.env`` есть и
указывает на gmail — поэтому :func:`delivery_status` честно отвечает ``'ok'``,
попытка отправки РЕАЛЬНО делается и падает через ~16 секунд
(``WinError 1225``: исходящий 587 с этого сервера закрыт).

Практический вывод один и тот же — письма не уходят, — но путь другой, и
модуль обязан выдерживать ОБА:

* сначала проверяем :func:`delivery_status`; если он не ``'ok'`` — **попытки
  соединения не делаем вовсе** (``aiosmtplib.send`` на пустом хосте это
  DNS-резолв и TCP-таймаут на ровном месте);
* если он ``'ok'``, но релей молчит — спасает :data:`_SEND_TIMEOUT`, а сам
  вызов вынесен в фоновую задачу после ответа посетителю;
* любой исход возвращается СТРОКОЙ и записывается на обращение, поэтому
  владелец видит «письма не было», а не гадает;
* ничего не бросает. Единственное исключение, которое имеет право сорвать
  отправку формы, — сбой записи самого обращения, а он случается раньше.

Паттерн подсмотрен в :mod:`app.social.notifications` (там почта тоже
best-effort поверх сайта), но код не переиспользован: там доставка привязана
к настройкам КОНКРЕТНОГО участника, его антиспам-окнам и его Telegram-боту,
а здесь адресат ровно один — владелец инстанса, и никакой per-user матрицы
каналов нет. Тащить сюда ту машинерию значило бы завести настройки, которых
никто не просил.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.logging_setup import get_logger

log = get_logger("persona.support.notify")

#: Потолок ожидания SMTP. ИЗМЕРЕНО, а не выбрано красиво: на этом сервере
#: исходящий 587 порт закрыт, и ``aiosmtplib`` возвращает ошибку соединения
#: только через ~16 секунд (WinError 1225). Без потолка это ожидание сидело бы
#: внутри обработки запроса — и «отправить обращение» превращалось бы в
#: полминуты белого экрана. Само уведомление владельцу ушло в фоновую задачу
#: (см. app/web/routes/support.py), но воркер тоже не должен висеть на сокете.
_SEND_TIMEOUT = 10.0

#: Сколько символов тела обращения уезжает в письмо. Полный текст всегда есть
#: на сайте; письмо — это «пришло обращение, вот суть, вот ссылка».
_EXCERPT = 1200


def _short(text: str, limit: int = _EXCERPT) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "\n\n[…обрезано, полностью — на сайте]"


async def _deliver(to_addr: str, subject: str, text: str) -> str:
    """Отправить письмо, вернув ЧЕСТНУЮ строку исхода.

    Возвращает ``'sent'`` | ``'skipped:<причина>'`` | ``'error:<причина>'``.
    Значение уходит прямо в БД и прямо на экран владельцу, поэтому оно должно
    читаться человеком без словаря.
    """
    if not to_addr:
        log.info("support.mail.skipped", reason="no_recipient")
        return "skipped:нет адреса получателя"

    # Импорт локальный: почтовый модуль тянет настройки и опциональный
    # aiosmtplib, а страница поддержки обязана открываться и без них.
    from app.smtp_delivery import delivery_status, send_email  # noqa: PLC0415

    try:
        status = await delivery_status()
    except Exception as exc:  # noqa: BLE001 — проверка конфига не роняет форму
        log.info("support.mail.status_failed", error=str(exc))
        return "error:не удалось прочитать настройки почты"

    if status != "ok":
        # Инстанс без ``.env``-фолбэка приходит сюда; с ним — идёт ниже и
        # получает отказ релея. Оба исхода записываются на обращение.
        log.info("support.mail.skipped", reason=status, to=to_addr)
        return f"skipped:почта не настроена ({status})"

    try:
        result: dict[str, Any] = await asyncio.wait_for(
            send_email(to_addr, subject, text), timeout=_SEND_TIMEOUT
        )
    except TimeoutError:
        log.warning("support.mail.timeout", to=to_addr, seconds=_SEND_TIMEOUT)
        return f"error:релей не ответил за {int(_SEND_TIMEOUT)} с"
    except Exception as exc:  # noqa: BLE001 — send_email и так не бросает, но
        # если однажды начнёт — посетитель не должен получить 500 из-за письма.
        log.warning("support.mail.raised", error=str(exc))
        return "error:сбой отправки"

    outcome = str(result.get("status") or "")
    if outcome == "sent":
        log.info("support.mail.sent", to=to_addr)
        return "sent"
    log.info("support.mail.not_sent", outcome=outcome, to=to_addr)
    if outcome == "error":
        return f"error:{str(result.get('error') or 'SMTP отверг письмо')[:80]}"
    return f"skipped:почта недоступна ({outcome or 'неизвестно'})"


async def notify_owner(ticket: dict[str, Any], owner_addr: str) -> str:
    """Best-effort письмо владельцу о новом обращении. Никогда не бросает."""
    author = ticket.get("email") or "аккаунт без адреса"
    subject = f"[Persona] Обращение #{ticket['id']}: {ticket.get('subject', '')}"[:180]
    text = (
        f"Новое обращение в поддержку.\n\n"
        f"Тема:     {ticket.get('subject', '')}\n"
        f"От:       {author}\n"
        f"Роль:     {ticket.get('role', 'anon')}\n"
        f"Страница: {ticket.get('source_page') or '—'}\n"
        f"Версия:   {ticket.get('app_version') or '—'}\n"
        f"Браузер:  {ticket.get('browser_class') or '—'}\n"
        f"\n{'-' * 40}\n\n"
        f"{_short(str(ticket.get('body', '')))}\n\n"
        f"{'-' * 40}\n"
        f"Ответить можно на сайте: /settings/support?ticket={ticket['id']}\n"
    )
    return await _deliver(owner_addr, subject, text)


async def reply_to_author(
    ticket: dict[str, Any], to_addr: str, reply_body: str
) -> str:
    """Best-effort письмо автору обращения с ответом владельца."""
    subject = f"Re: {ticket.get('subject', '')} (обращение #{ticket['id']})"[:180]
    text = (
        f"{reply_body.strip()}\n\n"
        f"{'-' * 40}\n"
        f"Это ответ на твоё обращение в поддержку Persona:\n"
        f"«{_short(str(ticket.get('body', '')), 300)}»\n"
    )
    return await _deliver(to_addr, subject, text)
