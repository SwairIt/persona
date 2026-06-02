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
    """Return the eight ``smtp_*`` rows, defaulting absent keys to ``''``."""
    async with get_connection() as conn:
        values: dict[str, str] = {}
        for key in _ALL_KEYS:
            raw = await get_kv(conn, key)
            values[key] = "" if raw is None else raw
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
) -> EmailMessage:
    """Compose a single multipart message from the rendered digest body."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body_markdown)
    if body_html:
        message.add_alternative(body_html, subtype="html")
    return message


async def send_digest_email(
    subject: str,
    body_markdown: str,
    body_html: str | None = None,
) -> dict[str, Any]:
    """Send a digest via the user-configured SMTP relay.

    Returns a status dict; never raises for configuration / network
    problems so callers can keep the daily scheduler alive:

    * ``{"status": "disabled"}`` — opt-in switch is off.
    * ``{"status": "missing_dep", "hint": "..."}`` — aiosmtplib not installed.
    * ``{"status": "misconfigured", "missing": [...]}`` — required rows blank.
    * ``{"status": "error", "error": "..."}`` — SMTP rejected the message.
    * ``{"status": "sent", "to": "..."}`` — relay accepted the envelope.
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
    )

    log.info(
        "smtp.send.attempt",
        host=host,
        port=port,
        to=recipient,
        starttls=use_tls,
        authenticated=bool(user),
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
