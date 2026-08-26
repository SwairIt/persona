"""SMTP delivery of daily / weekly LLM digests (v0.31).

The user configures their own SMTP relay through ``/settings/smtp`` —
Persona does *not* run a mailer of its own. All eight knobs live in the
``kv_settings`` table (seeded by migration ``030_smtp_settings.sql``):

* ``smtp_host``, ``smtp_port`` — relay endpoint
* ``smtp_user``, ``smtp_pass`` — login credentials
* ``smtp_to``, ``smtp_from`` — envelope addresses
* ``smtp_tls`` — ``'true'`` to use STARTTLS on the configured port,
  ``'false'`` to send in the clear (rarely what you want)
* ``smtp_enabled`` — top-level opt-in switch

:func:`send_digest_email` is the single public entrypoint. It returns a
status dict instead of raising on configuration problems so callers
(e.g. the daily-digest worker) can log and continue without crashing
the whole scheduler. The only outcomes that raise are genuine
programming errors — runtime SMTP failures are caught and surfaced as
``{"status": "error", "error": "..."}``.

The optional dependency ``aiosmtplib`` is imported lazily so that the
rest of the app keeps working on installs that have not opted in to
SMTP delivery.

v2.33 — КАК письмо уходит, решает :mod:`app.mail_transport` (kv
``mail_transport`` / env ``PERSONA_MAIL_TRANSPORT``): ``smtp_starttls``
(дефолт = сегодняшнее поведение), ``smtp_ssl`` (implicit TLS) или
``http_api`` (HTTPS-провайдер). Понадобилось не из любви к абстракциям: на
этом сервере исходящие 25/465/587/2525 закрыты ФАЙРВОЛОМ ПО ПОРТУ (проверено
на одном и том же IP: 443/2053/2087/8443 открыты, 25/465/587/2525 — отказ),
поэтому никакой SMTP-релей на 587 тут работать не может в принципе. Этот
модуль остался тем же, чем был, — местом, где живут НАСТРОЙКИ и СТАТУС-
КОНТРАКТ; сокеты и HTTP переехали в транспорт.

Контракт статусов (расширен, ни одно старое значение не изменило смысла):

* ``disabled`` / ``misconfigured`` / ``missing_dep`` — как раньше;
* ``no_credentials`` — транспорт ``http_api`` без ключа провайдера;
* ``unreachable`` — конфиг полный, но до транспорта НЕ ДОХОДИТ TCP. Именно
  это состояние прод и изображал как ``ok``, из-за чего страница обещала
  письмо, которого не будет;
* ``ok`` — сконфигурировано И достижимо.
"""

from __future__ import annotations

from typing import Any

from app import mail_transport
from app.logging_setup import get_logger
from app.mail_transport import MailConfig, MailMessage
from app.storage.db import get_connection
from app.storage.repository import get_kv

log = get_logger("persona.smtp")

# Eight kv_settings keys, plus the subset that must be non-empty before
# we will even attempt to connect. ``smtp_user`` / ``smtp_pass`` are not
# strictly required (some relays accept anonymous submission from the
# loopback) so we don't list them as hard requirements.
_REQUIRED_KEYS: tuple[str, ...] = ("smtp_host", "smtp_port", "smtp_to", "smtp_from")
_ALL_KEYS: tuple[str, ...] = (
    "smtp_enabled",
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_pass",
    "smtp_to",
    "smtp_from",
    "smtp_tls",
    # Не ``smtp_*``, но резолвится ровно так же (kv выигрывает, env — дефолт),
    # поэтому читается вместе с остальными, а не отдельной веткой.
    "mail_transport",
)

_MISSING_DEP_HINT = (
    "aiosmtplib is required for SMTP delivery. "
    "Install it with `uv pip install aiosmtplib` and restart Persona."
)

#: What a *transactional* message (magic link, password reset — see
#: :func:`send_email`) needs. ``smtp_to`` is not here: the recipient is an
#: argument, not a setting.
_TRANSACTIONAL_KEYS: tuple[str, ...] = ("smtp_host", "smtp_port", "smtp_from")

#: ``http_api`` не ходит по SMTP, поэтому ``smtp_host``/``smtp_port`` для него
#: не значат ничего и требовать их — врать в лицо. Адрес отправителя нужен
#: по-прежнему: он уезжает в поле ``from`` провайдера.
_HTTP_TRANSACTIONAL_KEYS: tuple[str, ...] = ("smtp_from",)
_HTTP_DIGEST_KEYS: tuple[str, ...] = ("smtp_from", "smtp_to")


def _required_keys(transport: str, *, transactional: bool) -> tuple[str, ...]:
    """Какие настройки обязаны быть непустыми ДЛЯ ЭТОГО транспорта."""
    if transport == mail_transport.HTTP_API:
        return _HTTP_TRANSACTIONAL_KEYS if transactional else _HTTP_DIGEST_KEYS
    return _TRANSACTIONAL_KEYS if transactional else _REQUIRED_KEYS


def _build_config(settings: dict[str, str]) -> MailConfig:
    """Собрать :class:`MailConfig` из разрешённых настроек.

    Единственное место, где строка настройки превращается в «как слать».
    Ключ провайдера сюда приходит из env/файла (:func:`mail_transport.read_api_key`)
    и НИКОГДА из ``settings`` — в БД его нет и не будет.
    """
    transport = mail_transport.resolve_transport(settings.get("mail_transport"))
    port_raw = settings.get("smtp_port", "")
    port = _parse_port(port_raw) if str(port_raw).strip() else (
        mail_transport.DEFAULT_PORTS[transport]
    )
    return MailConfig(
        transport=transport,
        host=settings.get("smtp_host", "").strip(),
        port=port,
        user=settings.get("smtp_user", "").strip(),
        password=settings.get("smtp_pass", ""),
        sender=settings.get("smtp_from", "").strip(),
        # ``smtp_tls`` сохраняет ровно свой прежний смысл на пути STARTTLS.
        starttls=settings.get("smtp_tls", "").strip().lower() == "true",
        api_key=(
            mail_transport.read_api_key()
            if transport == mail_transport.HTTP_API
            else ""
        ),
        timeout=mail_transport.resolve_timeout(),
    )


async def _resolve_state(
    *, transactional: bool
) -> tuple[str, dict[str, str], MailConfig, list[str]]:
    """Разрешить конфиг ОДИН раз и ответить «можем ли мы вообще слать».

    Возвращает ``(status, settings, config, missing)``. Единственный источник
    правды и для :func:`send_email`, и для :func:`send_digest_email`, и для
    :func:`delivery_status` — «узнать» и «попробовать» не могут разъехаться.
    """
    settings = await _load_settings()
    cfg = _build_config(settings)
    if settings["smtp_enabled"].strip().lower() != "true":
        return "disabled", settings, cfg, []
    missing = [
        k
        for k in _required_keys(cfg.transport, transactional=transactional)
        if not settings.get(k, "").strip()
    ]
    if missing:
        return "misconfigured", settings, cfg, missing
    if cfg.is_http:
        if not cfg.api_key:
            return "no_credentials", settings, cfg, []
    else:
        try:
            import aiosmtplib  # noqa: F401, PLC0415 — presence probe for the optional dep
        except ImportError:
            return "missing_dep", settings, cfg, []
    # Последняя и самая честная проверка: доходит ли до транспорта TCP вообще.
    # Без неё ``ok`` означало «поля заполнены», а не «письмо уйдёт», — и прод
    # обещал посетителю письмо, которое умирало через 16 секунд.
    ok, reason = await mail_transport.reachable(cfg)
    if not ok:
        return "unreachable", settings, cfg, [reason] if reason else []
    return "ok", settings, cfg, []


def _refusal(status: str, missing: list[str]) -> dict[str, Any]:
    """Статус-словарь для исхода, в котором отправлять НЕ надо и НЕ пробуем."""
    if status == "misconfigured":
        return {"status": "misconfigured", "missing": missing}
    if status == "missing_dep":
        return {"status": "missing_dep", "hint": _MISSING_DEP_HINT}
    if status == "no_credentials":
        return {"status": "no_credentials", "hint": mail_transport.credential_hint()}
    if status == "unreachable":
        # Причина — имя класса исключения сокета, не значение настройки.
        reason = missing[0] if missing else "connect failed"
        return {"status": "unreachable", "error": f"транспорт недоступен ({reason})"}
    return {"status": status}


async def _load_settings() -> dict[str, str]:
    """``smtp_*`` из ``kv_settings``; пустые ключи добираются из env/.env
    (``PERSONA_SMTP_*`` через ``Settings``). Правило проекта: kv выигрывает,
    Settings (env-loaded) — дефолт. Так владелец может положить креды в ``.env``
    (вне git), не открывая UI ``/settings/smtp``."""
    from app.settings import get_settings

    settings = get_settings()
    async with get_connection() as conn:
        values: dict[str, str] = {}
        for key in _ALL_KEYS:
            raw = await get_kv(conn, key)
            if raw is None or not str(raw).strip():
                raw = getattr(settings, key, "") or ""  # .env fallback (PERSONA_<KEY>)
            values[key] = "" if raw is None else str(raw)
    # Gmail-friendly: пустые from/to по умолчанию = user — чтобы для рабочей
    # отправки хватило заполнить только USER+PASS (smtp_to всё ещё в required-keys).
    user = values.get("smtp_user", "").strip()
    if user and not values.get("smtp_from", "").strip():
        values["smtp_from"] = user
    if user and not values.get("smtp_to", "").strip():
        values["smtp_to"] = user
    return values


def _missing_keys(settings: dict[str, str]) -> list[str]:
    """Return required keys whose value is empty / whitespace-only."""
    return [key for key in _REQUIRED_KEYS if not settings.get(key, "").strip()]


def _parse_port(raw: str) -> int:
    """Parse ``smtp_port``. Falls back to 587 if the row is bogus."""
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return 587


async def send_digest_email(
    subject: str,
    body_markdown: str,
    body_html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> dict[str, Any]:
    """Send a digest via the user-configured transport.

    Returns a status dict; never raises for configuration / network
    problems so callers can keep the daily scheduler alive:

    * ``{"status": "disabled"}`` — opt-in switch is off.
    * ``{"status": "missing_dep", "hint": "..."}`` — aiosmtplib not installed.
    * ``{"status": "misconfigured", "missing": [...]}`` — required rows blank.
    * ``{"status": "no_credentials", "hint": "..."}`` — ``http_api`` без ключа.
    * ``{"status": "unreachable", "error": "..."}`` — до транспорта нет TCP.
    * ``{"status": "error", "error": "..."}`` — the transport rejected it.
    * ``{"status": "timeout", "error": "..."}`` — потолок времени сработал.
    * ``{"status": "sent", "to": "..."}`` — the transport accepted the envelope.

    ``attachments`` is an optional list of ``(filename, content_bytes,
    mime_type)`` triples (added v0.57 for the weekly stats CSV worker).
    Callers that do not need attachments can omit the argument entirely
    — the parameter is keyword-defaulted so existing call sites stay
    binary-compatible.
    """
    status, settings, cfg, missing = await _resolve_state(transactional=False)
    if status != "ok":
        if status == "disabled":
            log.debug("smtp.send.skipped", reason="disabled")
        else:
            log.warning("smtp.send.refused", reason=status, missing=missing)
        return _refusal(status, missing)

    recipient = settings["smtp_to"].strip()
    return await mail_transport.deliver(
        cfg,
        MailMessage(
            recipient=recipient,
            subject=subject,
            text=body_markdown,
            html=body_html,
            attachments=list(attachments or []),
        ),
    )


async def _transactional_state() -> tuple[str, dict[str, str], list[str]]:
    """Обратно-совместимая обёртка над :func:`_resolve_state`.

    Была единственным источником правды до появления транспортов; осталась,
    потому что читается снаружи, и потому что «узнать» и «попробовать» обязаны
    оставаться одним и тем же кодом.
    """
    status, settings, _cfg, missing = await _resolve_state(transactional=True)
    return status, settings, missing


async def delivery_status() -> str:
    """Can this instance deliver email *at all* right now?

    ``"ok"`` means the configuration is complete and the optional dependency
    is installed — a send would at least be attempted. Every other value
    (``"disabled"`` / ``"misconfigured"`` / ``"missing_dep"``) means no mail
    can leave this box, so nothing that depends on a delivered message (a
    magic link, hence email verification) is reachable here.

    Note that ``smtp_enabled='true'`` with an empty ``smtp_host`` is
    ``"misconfigured"``, not ``"ok"``: the switch being on says nothing about
    whether a relay was ever filled in. Config is resolved by
    :func:`_load_settings` (kv wins, ``.env`` is the fallback), so this
    answers for the same settings a real send would use.

    Raises whatever the config read raises — callers decide how to fail.
    """
    status, _settings, _missing = await _transactional_state()
    return status


async def send_email(
    to_addr: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> dict[str, Any]:
    """Send a one-off transactional email to an arbitrary recipient.

    Unlike :func:`send_digest_email` (which mails the configured
    ``smtp_to``), this sends to ``to_addr`` — used for magic-link login and
    other per-user notifications. Same config + same status-dict contract;
    never raises for config/network problems. ``smtp_to`` is NOT required
    here (the recipient is the argument).

    Транспорт выбирается настройкой (см. :mod:`app.mail_transport`); внешний
    контракт функции не изменился — вызывающие продолжают смотреть только на
    ``result["status"]``.
    """
    status, _settings, cfg, missing = await _resolve_state(transactional=True)
    if status != "ok":
        log.info("smtp.send.refused", reason=status, missing=missing)
        return _refusal(status, missing)
    return await mail_transport.deliver(
        cfg,
        MailMessage(
            recipient=to_addr, subject=subject, text=body_text, html=body_html
        ),
    )
