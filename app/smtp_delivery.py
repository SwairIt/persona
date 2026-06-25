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
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

from app.logging_setup import get_logger
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
)

_MISSING_DEP_HINT = (
    "aiosmtplib is required for SMTP delivery. "
    "Install it with `uv pip install aiosmtplib` and restart Persona."
)


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


def _build_message(
    *,
    sender: str,
    recipient: str,
    subject: str,
    body_markdown: str,
    body_html: str | None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> EmailMessage:
    """Compose a single multipart message from the rendered digest body.

    ``attachments`` is a list of ``(filename, content_bytes, mime_type)``
    triples. Each entry is added via :meth:`EmailMessage.add_attachment`
    with the MIME type split into ``maintype/subtype``. Bogus MIME types
    fall back to ``application/octet-stream`` so a malformed caller does
    not blow up the whole send.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body_markdown)
    if body_html:
        message.add_alternative(body_html, subtype="html")
    for filename, content, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )
    return message


async def send_digest_email(
    subject: str,
    body_markdown: str,
    body_html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> dict[str, Any]:
    """Send a digest via the user-configured SMTP relay.

    Returns a status dict; never raises for configuration / network
    problems so callers can keep the daily scheduler alive:

    * ``{"status": "disabled"}`` — opt-in switch is off.
    * ``{"status": "missing_dep", "hint": "..."}`` — aiosmtplib not installed.
    * ``{"status": "misconfigured", "missing": [...]}`` — required rows blank.
    * ``{"status": "error", "error": "..."}`` — SMTP rejected the message.
    * ``{"status": "sent", "to": "..."}`` — relay accepted the envelope.

    ``attachments`` is an optional list of ``(filename, content_bytes,
    mime_type)`` triples (added v0.57 for the weekly stats CSV worker).
    Callers that do not need attachments can omit the argument entirely
    — the parameter is keyword-defaulted so existing call sites stay
    binary-compatible.
    """
    settings = await _load_settings()

    if settings["smtp_enabled"].strip().lower() != "true":
        log.debug("smtp.send.skipped", reason="disabled")
        return {"status": "disabled"}

    missing = _missing_keys(settings)
    if missing:
        log.warning("smtp.send.misconfigured", missing=missing)
        return {"status": "misconfigured", "missing": missing}

    try:
        import aiosmtplib  # noqa: PLC0415 — optional dep, imported lazily
    except ImportError:
        log.error("smtp.send.missing_dep")
        return {"status": "missing_dep", "hint": _MISSING_DEP_HINT}

    host = settings["smtp_host"].strip()
    port = _parse_port(settings["smtp_port"])
    user = settings["smtp_user"].strip()
    password = settings["smtp_pass"]
    recipient = settings["smtp_to"].strip()
    sender = settings["smtp_from"].strip()
    use_tls = settings["smtp_tls"].strip().lower() == "true"

    message = _build_message(
        sender=sender,
        recipient=recipient,
        subject=subject,
        body_markdown=body_markdown,
        body_html=body_html,
        attachments=attachments,
    )

    log.info(
        "smtp.send.attempt",
        host=host,
        port=port,
        to=recipient,
        starttls=use_tls,
        authenticated=bool(user),
        attachments=len(attachments or []),
    )

    try:
        await aiosmtplib.send(
            message,
            hostname=host,
            port=port,
            start_tls=use_tls,
            username=user or None,
            password=password or None,
        )
    except Exception as exc:
        log.warning("smtp.send.failed", error=str(exc), host=host, port=port)
        return {"status": "error", "error": str(exc)}

    log.info("smtp.send.ok", to=recipient)
    return {"status": "sent", "to": recipient}


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
    """
    settings = await _load_settings()

    if settings["smtp_enabled"].strip().lower() != "true":
        return {"status": "disabled"}

    missing = [
        k for k in ("smtp_host", "smtp_port", "smtp_from")
        if not settings.get(k, "").strip()
    ]
    if missing:
        return {"status": "misconfigured", "missing": missing}

    try:
        import aiosmtplib  # noqa: PLC0415 — optional dep, imported lazily
    except ImportError:
        return {"status": "missing_dep", "hint": _MISSING_DEP_HINT}

    host = settings["smtp_host"].strip()
    port = _parse_port(settings["smtp_port"])
    user = settings["smtp_user"].strip()
    password = settings["smtp_pass"]
    sender = settings["smtp_from"].strip()
    use_tls = settings["smtp_tls"].strip().lower() == "true"

    message = _build_message(
        sender=sender,
        recipient=to_addr,
        subject=subject,
        body_markdown=body_text,
        body_html=body_html,
    )
    log.info("smtp.send.attempt", host=host, port=port, to=to_addr, starttls=use_tls)
    try:
        await aiosmtplib.send(
            message,
            hostname=host,
            port=port,
            start_tls=use_tls,
            username=user or None,
            password=password or None,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("smtp.send.failed", error=str(exc), host=host, port=port)
        return {"status": "error", "error": str(exc)}

    log.info("smtp.send.ok", to=to_addr)
    return {"status": "sent", "to": to_addr}
