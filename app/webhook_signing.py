"""HMAC-SHA256 signing for outbound Persona webhooks.

Receivers verify authenticity of every webhook delivery by recomputing
``HMAC-SHA256(secret, raw_body_bytes)`` and comparing it (constant-time)
to the ``X-Persona-Signature`` header. Together with the
``X-Persona-Timestamp`` header receivers can also reject replays.

This module is the single source of truth for the signature wire format
``"sha256=<hexdigest>"``. The dispatcher imports :func:`sign_payload` and
:func:`ensure_secret`; tests and any future receiver-side helper should
use :func:`verify_payload`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.webhook.sign")

_SIGNATURE_PREFIX = "sha256="
_SECRET_BYTES = 32


def sign_payload(secret: str, body: bytes) -> str:
    """Return the wire-format signature for ``body`` keyed by ``secret``.

    The returned string always starts with ``sha256=`` so that future
    algorithms (e.g. ``sha512=``) can coexist on the same header.
    Empty secrets are rejected — callers must run
    :func:`ensure_secret` first.
    """
    if not secret:
        msg = "refusing to sign payload with empty secret"
        raise ValueError(msg)
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return _SIGNATURE_PREFIX + digest


def verify_payload(secret: str, body: bytes, header_value: str) -> bool:
    """Constant-time check that ``header_value`` matches HMAC of ``body``.

    Accepts the wire format ``sha256=<hex>`` and also the bare hex form
    (some receiver frameworks strip prefixes). Returns ``False`` on any
    parse failure rather than raising — verification must never leak
    information about *why* a signature was rejected.
    """
    if not secret or not header_value:
        return False
    candidate = header_value.strip()
    if candidate.startswith(_SIGNATURE_PREFIX):
        candidate = candidate[len(_SIGNATURE_PREFIX) :]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, candidate)


async def ensure_secret(webhook_id: int) -> str:
    """Return the row's signing secret, generating one if absent.

    "Absent" means either NULL or empty string — migration 028 normalises
    legacy NULL rows to ``''`` so the dispatcher only has to check for
    falsiness. On generation we update the row in-place and log
    ``persona.webhook.sign.secret_generated`` so operators can correlate
    with the receiver side.
    """
    async with get_connection() as conn:
        existing = await _read_secret(conn, webhook_id)
        if existing:
            return existing
        new_secret = secrets.token_urlsafe(_SECRET_BYTES)
        await conn.execute(
            "UPDATE webhooks SET secret = ? WHERE id = ?",
            (new_secret, webhook_id),
        )
        await conn.commit()
        log.info("webhook.sign.secret_generated", webhook_id=webhook_id)
        return new_secret


async def _read_secret(conn: aiosqlite.Connection, webhook_id: int) -> str:
    cursor = await conn.execute(
        "SELECT secret FROM webhooks WHERE id = ?",
        (webhook_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        msg = f"webhook {webhook_id} not found"
        raise LookupError(msg)
    value = row["secret"]
    return "" if value is None else str(value)
