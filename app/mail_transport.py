"""Как именно письмо покидает этот сервер — выбор транспорта, а не переписывание.

ПОЧЕМУ МОДУЛЬ ВООБЩЕ ПОЯВИЛСЯ (измерено на этом сервере, 2026-08-26, не
угадано). Исходящий фильтр режет письма **по порту назначения, а не по хосту**:

===========================  ======  ===============================
назначение                   порт    результат
===========================  ======  ===============================
smtp.gmail.com               25/465/587  ConnectionRefused за ~1.2 с
smtp.yandex.ru               465/587     ConnectionRefused
smtp.mail.ru                 465         ConnectionRefused
любой хост                   2525        ConnectionRefused
api.resend.com (Cloudflare)  443/2053/2087/8443  открыт, TLS-сертификат настоящий
smtp.resend.com              2587        открыт, баннер «220 Resend SMTP Relay»
smtp.resend.com              2465        открыт (implicit TLS)
===========================  ======  ===============================

Решающая проверка — один и тот же IP Cloudflare: 443/2053/2087/2096/8443
соединяются, а 25/465/587/2525 отвергаются. Значит блокирует не «гугл» и не
«заграница», а список SMTP-портов. Отсюда два вывода, на которых стоит весь
модуль:

1. «Починить SMTP» на классическом 587 с этого сервера **невозможно** — сколько
   провайдера ни меняй. Нужен либо нестандартный порт submission, либо HTTPS.
2. Поэтому транспорт обязан быть **настройкой**, а не веткой в коде: одна и та
   же сборка должна уметь и 587 (там, где он открыт), и 465, и HTTPS-API.

Транспорты (kv ``mail_transport`` / env ``PERSONA_MAIL_TRANSPORT``):

``smtp_starttls``
    ДЕФОЛТ — ровно сегодняшнее поведение (``aiosmtplib.send`` с STARTTLS на
    ``smtp_port``). Ничего не меняется у того, у кого уже работает.
``smtp_ssl``
    Implicit TLS: TLS с первого байта, без STARTTLS. Обычно 465 — но именно
    здесь живёт рабочий обход, ``smtp.resend.com:2465``.
``http_api``
    Письмо уходит POST'ом на HTTPS (порт 443), которому этот фильтр не мешает.
    Провайдер ОДИН и сделан честно — Resend (см. :data:`HTTP_API_URL`).

Почему Resend, а не Unisender/SendGrid/Mailgun/Yandex Postbox: он единственный
из проверенных, кто с этого сервера достижим **обоими** способами (HTTPS-API и
submission на 2465/2587), то есть даёт запасной путь, если однажды закроют 443
к нему или наоборот. Плюс регистрация без карты и без юрлица (GitHub-логин),
а отправка — один POST с ``Authorization: Bearer``, без SigV4 (Yandex Postbox)
и без онбординга юрлица (Unisender). Ограничение free-тарифа честно описано в
:func:`credential_hint`.

ПРАВИЛА, КОТОРЫЕ ЗДЕСЬ НЕ ОБСУЖДАЮТСЯ:

* **Никогда не бросать в запрос.** Любой исход — статус-словарь.
* **Жёсткий потолок времени.** Тот самый WinError 1225 возвращался через ~16 с;
  столько посетитель ждать не должен. :data:`DEFAULT_TIMEOUT` = 10 с, и он
  накрывает всю операцию (``asyncio.wait_for``), а не только сокет.
* **Ключ не живёт в БД и не попадает в логи.** Только env
  ``PERSONA_RESEND_API_KEY`` или файл ``{PERSONA_DATA_DIR}/resend_api_key``.
  Всё, что уезжает наружу (лог, статус-словарь), проходит :func:`scrub`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import socket
import time
from dataclasses import dataclass, field
from email.message import EmailMessage

from app.logging_setup import get_logger

log = get_logger("persona.mail.transport")

SMTP_STARTTLS = "smtp_starttls"
SMTP_SSL = "smtp_ssl"
HTTP_API = "http_api"

#: Всё, что можно положить в ``mail_transport``.
TRANSPORTS: tuple[str, ...] = (SMTP_STARTTLS, SMTP_SSL, HTTP_API)

#: Дефолт = сегодняшнее поведение. Пустое/незнакомое значение резолвится сюда,
#: поэтому обновление кода без правки настроек ничего не ломает.
DEFAULT_TRANSPORT = SMTP_STARTTLS

#: Порт по умолчанию, если ``smtp_port`` пуст/мусорный.
DEFAULT_PORTS: dict[str, int] = {SMTP_STARTTLS: 587, SMTP_SSL: 465, HTTP_API: 443}

HTTP_API_PROVIDER = "resend"
HTTP_API_HOST = "api.resend.com"
HTTP_API_URL = "https://api.resend.com/emails"

#: Имя файла с ключом внутри ``PERSONA_DATA_DIR`` (вне репозитория).
API_KEY_FILENAME = "resend_api_key"
API_KEY_ENV = "PERSONA_RESEND_API_KEY"

#: Потолок на ВСЮ отправку. Измерен, а не выбран: отказ 587 приходил через ~16 с.
DEFAULT_TIMEOUT = 10.0
_MIN_TIMEOUT = 1.0
_MAX_TIMEOUT = 60.0

#: Проба достижимости — отдельный, более жёсткий потолок: она сидит на пути
#: ``delivery_status()``, который дёргается при отрисовке страниц.
PROBE_TIMEOUT = 3.0
PROBE_TTL = 300.0

_probe_cache: dict[tuple[str, str, int], tuple[bool, str, float]] = {}


# ── конфиг ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MailConfig:
    """Разрешённая конфигурация отправки — всё, что нужно любому транспорту."""

    transport: str = DEFAULT_TRANSPORT
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    starttls: bool = True
    api_key: str = ""
    timeout: float = DEFAULT_TIMEOUT

    @property
    def is_http(self) -> bool:
        return self.transport == HTTP_API


@dataclass
class MailMessage:
    """Содержимое письма, независимое от способа доставки."""

    recipient: str
    subject: str
    text: str
    html: str | None = None
    #: ``(filename, content_bytes, mime_type)`` — как у ``send_digest_email``.
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)


def resolve_transport(raw: str | None) -> str:
    """Нормализовать значение настройки в один из :data:`TRANSPORTS`.

    Незнакомое или пустое значение — это НЕ ошибка и НЕ отказ отправлять: оно
    резолвится в :data:`DEFAULT_TRANSPORT`. Опечатка в конфиге не должна
    выключать почту молча — она должна оставить её ровно такой, какой была.
    """
    value = str(raw or "").strip().lower().replace("-", "_")
    if value in TRANSPORTS:
        return value
    # Мелкие человеческие синонимы: «ssl»/«465», «starttls»/«587», «api»/«http».
    aliases = {
        "ssl": SMTP_SSL, "tls": SMTP_SSL, "smtps": SMTP_SSL, "465": SMTP_SSL,
        "starttls": SMTP_STARTTLS, "smtp": SMTP_STARTTLS, "587": SMTP_STARTTLS,
        "api": HTTP_API, "http": HTTP_API, "https": HTTP_API,
        HTTP_API_PROVIDER: HTTP_API,
    }
    if value in aliases:
        return aliases[value]
    if value:
        log.warning("mail.transport.unknown", value=value[:32], using=DEFAULT_TRANSPORT)
    return DEFAULT_TRANSPORT


def resolve_timeout(raw: str | None = None) -> float:
    """Потолок на отправку. env ``PERSONA_MAIL_TIMEOUT``, зажат в [1, 60] с."""
    source = raw if raw is not None else os.environ.get("PERSONA_MAIL_TIMEOUT", "")
    try:
        value = float(str(source).strip())
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, value))


def read_api_key() -> str:
    """Ключ HTTPS-провайдера. **Только env или файл — никогда не БД.**

    Порядок: ``PERSONA_RESEND_API_KEY`` → ``{PERSONA_DATA_DIR}/resend_api_key``.
    Возвращается сырое значение; наружу его отдавать нельзя — см. :func:`scrub`.
    """
    from_env = os.environ.get(API_KEY_ENV, "").strip()
    if from_env:
        return from_env
    try:
        from app.settings import get_settings  # noqa: PLC0415 — избегаем цикла

        path = get_settings().data_dir / API_KEY_FILENAME
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except Exception as exc:  # отсутствие ключа не роняет почту
        log.debug("mail.api_key.read_failed", error=type(exc).__name__)
    return ""


def scrub(text: str, *secrets: str) -> str:
    """Вырезать секреты из строки, которая уедет в лог или в статус-словарь.

    Провайдер не обязан возвращать наш ключ в теле ошибки — но «не обязан» это
    не «не может», а ключ, однажды попавший в лог, оттуда уже не достать.
    """
    out = str(text)
    for secret in secrets:
        token = str(secret or "").strip()
        if len(token) >= 6 and token in out:
            out = out.replace(token, "***")
    return out


def endpoint(cfg: MailConfig) -> tuple[str, int]:
    """Куда транспорт реально пойдёт по TCP — то, что и надо пробовать."""
    if cfg.is_http:
        return HTTP_API_HOST, 443
    return cfg.host, cfg.port


# ── достижимость ────────────────────────────────────────────────────────────


def probing_enabled() -> bool:
    """Разрешена ли сетевая проба. ``PERSONA_MAIL_PROBE=0`` выключает.

    Выключена в тестах (``tests/conftest.py``): набор не ходит в сеть.
    """
    return str(os.environ.get("PERSONA_MAIL_PROBE", "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def reset_probe_cache() -> None:
    """Забыть результаты проб (тесты и ручная перепроверка после правки конфига)."""
    _probe_cache.clear()


async def _tcp_reachable(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """Просто «пускает ли сюда TCP». Ничего не отправляем, сразу закрываем."""
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError:
        return False, "timeout"
    except (OSError, socket.gaierror) as exc:
        # WinError 1225/10061 (отказ), 11001 (нет DNS) — всё сюда.
        return False, type(exc).__name__
    except Exception as exc:  # проба не имеет права ронять вызов
        return False, type(exc).__name__
    with contextlib.suppress(Exception):  # закрытие сокета никого не волнует
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    del reader
    return True, ""


async def reachable(cfg: MailConfig) -> tuple[bool, str]:
    """Достижим ли транспорт прямо сейчас. Результат кэшируется на :data:`PROBE_TTL`.

    Кэшируется и ОТРИЦАТЕЛЬНЫЙ ответ — в этом весь смысл: закрытый порт отвечает
    отказом за ~1.2 с, и платить это на каждой отрисовке страницы нельзя.
    """
    if not probing_enabled():
        return True, ""
    host, port = endpoint(cfg)
    if not host:
        return False, "no_host"
    key = (cfg.transport, host, port)
    now = time.monotonic()
    cached = _probe_cache.get(key)
    if cached is not None and now - cached[2] < PROBE_TTL:
        return cached[0], cached[1]
    ok, reason = await _tcp_reachable(host, port, PROBE_TIMEOUT)
    _probe_cache[key] = (ok, reason, now)
    if not ok:
        log.warning("mail.transport.unreachable", transport=cfg.transport,
                    host=host, port=port, reason=reason)
    return ok, reason


# ── отправка ────────────────────────────────────────────────────────────────


def build_mime(cfg: MailConfig, msg: MailMessage) -> EmailMessage:
    """Собрать MIME-письмо для SMTP-транспортов."""
    mime = EmailMessage()
    mime["From"] = cfg.sender
    mime["To"] = msg.recipient
    mime["Subject"] = msg.subject
    mime.set_content(msg.text)
    if msg.html:
        mime.add_alternative(msg.html, subtype="html")
    for filename, content, mime_type in msg.attachments:
        maintype, _, subtype = str(mime_type).partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        mime.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return mime


async def _send_smtp(cfg: MailConfig, msg: MailMessage) -> dict[str, object]:
    """Оба SMTP-пути. Отличие ровно одно: STARTTLS против implicit TLS."""
    import aiosmtplib  # noqa: PLC0415 — опциональная зависимость, проверена выше

    implicit = cfg.transport == SMTP_SSL
    await aiosmtplib.send(
        build_mime(cfg, msg),
        hostname=cfg.host,
        port=cfg.port,
        # Взаимоисключающие: aiosmtplib ругается, если задать оба.
        use_tls=implicit,
        start_tls=(cfg.starttls if not implicit else None),
        username=cfg.user or None,
        password=cfg.password or None,
        timeout=cfg.timeout,
    )
    return {"status": "sent", "to": msg.recipient}


def _resend_payload(cfg: MailConfig, msg: MailMessage) -> dict[str, object]:
    """Тело POST /emails. Вложения — base64, как требует провайдер."""
    payload: dict[str, object] = {
        "from": cfg.sender,
        "to": [msg.recipient],
        "subject": msg.subject,
        "text": msg.text,
    }
    if msg.html:
        payload["html"] = msg.html
    if msg.attachments:
        payload["attachments"] = [
            {
                "filename": filename,
                "content": base64.b64encode(content).decode("ascii"),
                "content_type": mime_type,
            }
            for filename, content, mime_type in msg.attachments
        ]
    return payload


async def _send_http_api(cfg: MailConfig, msg: MailMessage) -> dict[str, object]:
    """Отправка по HTTPS. Единственный путь, который на этом сервере работает."""
    import httpx  # noqa: PLC0415 — тяжёлый импорт держим локальным

    async with httpx.AsyncClient(timeout=cfg.timeout) as client:
        response = await client.post(
            HTTP_API_URL,
            json=_resend_payload(cfg, msg),
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
        )
    if response.status_code < 300:
        body: object = {}
        with contextlib.suppress(Exception):  # id нужен только для лога
            body = response.json()
        message_id = str(body.get("id") or "") if isinstance(body, dict) else ""
        log.info("mail.http_api.sent", provider=HTTP_API_PROVIDER, message_id=message_id)
        return {"status": "sent", "to": msg.recipient, "message_id": message_id}

    # Ошибку показываем владельцу — значит она обязана быть читаемой и чистой.
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("name") or "")
    except Exception:  # тело не JSON — покажем сырой текст ниже
        detail = ""
    if not detail:
        detail = response.text[:200]
    detail = scrub(detail, cfg.api_key, cfg.password)
    log.warning(
        "mail.http_api.rejected",
        provider=HTTP_API_PROVIDER,
        http_status=response.status_code,
        error=detail[:200],
    )
    return {
        "status": "error",
        "error": f"{HTTP_API_PROVIDER} HTTP {response.status_code}: {detail[:200]}",
    }


async def deliver(cfg: MailConfig, msg: MailMessage) -> dict[str, object]:
    """Отправить письмо выбранным транспортом. **Никогда не бросает.**

    Возвращает ``{"status": "sent"|"error"|"timeout", ...}``. Потолок
    ``cfg.timeout`` накрывает всю операцию целиком — включая DNS-резолв и
    TLS-рукопожатие, которые внутрь сокетного таймаута библиотеки не входят.
    """
    sender = _send_http_api if cfg.is_http else _send_smtp
    host, port = endpoint(cfg)
    log.info(
        "mail.send.attempt",
        transport=cfg.transport,
        host=host,
        port=port,
        to=msg.recipient,
        authenticated=bool(cfg.api_key or cfg.user),
        attachments=len(msg.attachments),
    )
    try:
        return await asyncio.wait_for(sender(cfg, msg), timeout=cfg.timeout)
    except TimeoutError:
        log.warning("mail.send.timeout", transport=cfg.transport, host=host,
                    port=port, seconds=cfg.timeout)
        return {
            "status": "timeout",
            "error": f"транспорт {cfg.transport} не ответил за {int(cfg.timeout)} с",
        }
    except Exception as exc:  # почта не имеет права ронять запрос
        error = scrub(str(exc), cfg.api_key, cfg.password)
        log.warning("mail.send.failed", transport=cfg.transport, host=host,
                    port=port, error=error[:200])
        return {"status": "error", "error": error[:300]}


def credential_hint() -> str:
    """Что владельцу сделать руками, чтобы ``http_api`` заработал."""
    return (
        f"Транспорт http_api выбран, но ключ {HTTP_API_PROVIDER} не найден. "
        f"Заведи ключ на resend.com (бесплатно, без карты) и положи его в "
        f"переменную {API_KEY_ENV} в .env ИЛИ в файл "
        f"{{PERSONA_DATA_DIR}}/{API_KEY_FILENAME}. В БД ключ не кладём."
    )
