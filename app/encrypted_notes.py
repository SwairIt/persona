"""Opt-in per-note body encryption for the standalone ``notes`` table (v0.45).

The user picks a master password per note. We derive a Fernet key with
``PBKDF2-HMAC-SHA256(password, salt, 100_000)`` over a fresh 16-byte
per-note salt, encrypt the plaintext body, and prepend the salt to the
Fernet token so the envelope is self-contained. SQLite then holds:

    * ``encrypted = 1``           — gate flag readers MUST check first
    * ``body      = ""``          — empty (the column is ``NOT NULL`` and
                                    the v0.45 migration deliberately did
                                    not rebuild the table to drop that)
    * ``ciphertext = salt||token`` — opaque envelope

Decryption never persists the plaintext; callers receive it once and may
choose to display it (and only display it). Every successful decrypt is
written to the audit log via :func:`app.audit.log_action` so an operator
can spot a brute-force attempt after the fact.

Like :mod:`app.vault`, ``cryptography`` is treated as an *optional*
dependency. The probe lives in :func:`_cryptography_available`; every
public mutator short-circuits with ``{"status": "missing_dep"}`` rather
than blowing up the import graph when ``cryptography`` is absent.

Structured logging (``persona.encrypted_notes``) never carries the
plaintext, the password, or the ciphertext bytes — only ``note_id``,
``status`` and counts.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Final

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.encrypted_notes")

# Per-note PBKDF2 salt. 16 bytes is the standard recommendation and matches
# the kv-vault implementation in :mod:`app.vault`; longer buys no real-world
# resistance against the parameters we use here.
_SALT_BYTES: Final[int] = 16
# PBKDF2 iteration count, fixed by the v0.45 task spec.
_KDF_ITERATIONS: Final[int] = 100_000
# Fernet requires exactly a 32-byte secret (URL-safe base64-encoded).
_FERNET_KEY_BYTES: Final[int] = 32

_MISSING_DEP_HINT: Final[str] = (
    "cryptography is required for encrypted notes. "
    "Install it with `uv pip install cryptography` and restart Persona."
)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class BadPassword(Exception):
    """Raised when :func:`decrypt_note` cannot unwrap the Fernet envelope.

    Covers three distinct failure modes that the caller treats
    identically: wrong password, tampered ciphertext, and rows whose
    ``ciphertext`` blob is too short to even contain the salt prefix.
    The single exception type avoids leaking a side-channel that would
    let an attacker distinguish "row corrupt" from "wrong password".
    """


# ---------------------------------------------------------------------------
# Optional-dep probe
# ---------------------------------------------------------------------------


def _cryptography_available() -> bool:
    """Return ``True`` iff the ``cryptography`` package can be imported."""
    try:
        import cryptography.fernet  # noqa: F401, PLC0415 — optional dep probe
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte URL-safe-base64 Fernet key from ``password`` + ``salt``.

    PBKDF2-HMAC-SHA256 with the fixed iteration count. Unlike
    :mod:`app.vault` we do *not* mix the row key (note id) into the
    derivation: notes have no stable string handle the user would type,
    and binding to the integer id would make backup/restore (which can
    renumber rows) silently break decryption. The fresh per-note salt
    plus Fernet's own authenticated encryption are sufficient to keep
    one decrypted note from helping decrypt another.
    """
    import base64  # noqa: PLC0415 — keep the module-level import surface tiny

    raw = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _KDF_ITERATIONS,
        dklen=_FERNET_KEY_BYTES,
    )
    return base64.urlsafe_b64encode(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def encrypt_note(note_id: int, password: str) -> dict[str, Any]:
    """Encrypt the body of ``note_id`` under ``password``.

    Reads the current plaintext from ``notes.body``, builds a
    ``salt(16) || fernet_token`` envelope, then flips the row to
    ``encrypted = 1`` / ``body = ""`` / ``ciphertext = <envelope>`` in a
    single ``UPDATE``.

    Returns one of:
        * ``{"status": "ok", "note_id": <id>, "bytes": <envelope_len>}``
        * ``{"status": "missing_dep", "hint": "..."}``
        * ``{"status": "not_found"}``
        * ``{"status": "already_encrypted"}``
        * ``{"status": "invalid", "error": "..."}``

    Never logs (or audits) the plaintext or the password.
    """
    if not password:
        log.warning("encrypted_notes.encrypt.invalid", note_id=note_id)
        return {"status": "invalid", "error": "password is required"}

    if not _cryptography_available():
        log.warning("encrypted_notes.encrypt.missing_dep", note_id=note_id)
        return {"status": "missing_dep", "hint": _MISSING_DEP_HINT}

    from cryptography.fernet import Fernet  # noqa: PLC0415 — optional dep

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT body, encrypted FROM notes WHERE id = ?",
            (int(note_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            log.info("encrypted_notes.encrypt.not_found", note_id=note_id)
            return {"status": "not_found"}
        if int(row["encrypted"]) == 1:
            log.info("encrypted_notes.encrypt.already", note_id=note_id)
            return {"status": "already_encrypted"}

        plaintext = str(row["body"])
        salt = os.urandom(_SALT_BYTES)
        key = _derive_fernet_key(password, salt)
        token = Fernet(key).encrypt(plaintext.encode("utf-8"))
        envelope = salt + token

        await conn.execute(
            """
            UPDATE notes
               SET encrypted  = 1,
                   body       = '',
                   ciphertext = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (envelope, int(note_id)),
        )
        await conn.commit()

    log.info("encrypted_notes.encrypt.ok", note_id=note_id, bytes=len(envelope))
    await log_action(
        action="encrypted_notes.encrypt",
        target=f"note:{note_id}",
        detail=f"bytes={len(envelope)}",
    )
    return {"status": "ok", "note_id": int(note_id), "bytes": len(envelope)}


async def decrypt_note(note_id: int, password: str) -> str:
    """Return the plaintext body of an encrypted note.

    Raises :class:`BadPassword` if ``password`` does not unwrap the
    envelope, or if the row is missing / not encrypted / corrupt. The
    plaintext return value is **never** logged; the structured log only
    records ``note_id`` + ``status``.

    Every call — successful or not — is written to the audit log so an
    operator can spot brute-force attempts after the fact.
    """
    if not password:
        await log_action(
            action="encrypted_notes.decrypt",
            target=f"note:{note_id}",
            detail="empty_password",
            success=False,
        )
        raise BadPassword("password is required")

    if not _cryptography_available():
        log.warning("encrypted_notes.decrypt.missing_dep", note_id=note_id)
        await log_action(
            action="encrypted_notes.decrypt",
            target=f"note:{note_id}",
            detail="missing_dep",
            success=False,
        )
        msg = "cryptography is not installed"
        raise BadPassword(msg)

    from cryptography.fernet import (  # noqa: PLC0415 — optional dep
        Fernet,
        InvalidToken,
    )

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT encrypted, ciphertext FROM notes WHERE id = ?",
            (int(note_id),),
        )
        row = await cursor.fetchone()

    if row is None:
        log.info("encrypted_notes.decrypt.not_found", note_id=note_id)
        await log_action(
            action="encrypted_notes.decrypt",
            target=f"note:{note_id}",
            detail="not_found",
            success=False,
        )
        msg = "note not found"
        raise BadPassword(msg)

    if int(row["encrypted"]) != 1 or row["ciphertext"] is None:
        log.info("encrypted_notes.decrypt.not_encrypted", note_id=note_id)
        await log_action(
            action="encrypted_notes.decrypt",
            target=f"note:{note_id}",
            detail="not_encrypted",
            success=False,
        )
        msg = "note is not encrypted"
        raise BadPassword(msg)

    envelope = bytes(row["ciphertext"])
    if len(envelope) <= _SALT_BYTES:
        log.warning(
            "encrypted_notes.decrypt.corrupt",
            note_id=note_id,
            bytes=len(envelope),
        )
        await log_action(
            action="encrypted_notes.decrypt",
            target=f"note:{note_id}",
            detail="corrupt_envelope",
            success=False,
        )
        msg = "ciphertext envelope is too short"
        raise BadPassword(msg)

    salt = envelope[:_SALT_BYTES]
    token = envelope[_SALT_BYTES:]
    key = _derive_fernet_key(password, salt)
    try:
        plaintext_bytes = Fernet(key).decrypt(token)
    except InvalidToken as exc:
        log.info("encrypted_notes.decrypt.bad_password", note_id=note_id)
        await log_action(
            action="encrypted_notes.decrypt",
            target=f"note:{note_id}",
            detail="bad_password",
            success=False,
        )
        msg = "wrong password or tampered ciphertext"
        raise BadPassword(msg) from exc

    log.info("encrypted_notes.decrypt.ok", note_id=note_id)
    await log_action(
        action="encrypted_notes.decrypt",
        target=f"note:{note_id}",
        detail="ok",
        success=True,
    )
    return plaintext_bytes.decode("utf-8")


async def list_encrypted() -> list[dict[str, Any]]:
    """Return every encrypted note's metadata — never any body or ciphertext.

    Ordered by ``updated_at DESC`` so the most recently locked rows come
    first. Output rows carry only the id, title, timestamps and a byte
    count of the envelope so a UI can render a "10 locked notes (4.2 KB)"
    summary without ever touching plaintext.
    """
    if not _cryptography_available():
        # Listing does not need the library, but surfacing the same
        # status string keeps callers' switch statements simple.
        log.info("encrypted_notes.list.missing_dep")

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, title, source, created_at, updated_at,
                   LENGTH(ciphertext) AS envelope_bytes
              FROM notes
             WHERE encrypted = 1
             ORDER BY updated_at DESC
            """,
        )
        rows = await cursor.fetchall()

    items = [
        {
            "id": int(row["id"]),
            "title": (str(row["title"]) if row["title"] is not None else None),
            "source": (str(row["source"]) if row["source"] is not None else None),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "envelope_bytes": int(row["envelope_bytes"] or 0),
        }
        for row in rows
    ]
    log.info("encrypted_notes.list", count=len(items))
    return items


__all__ = [
    "BadPassword",
    "decrypt_note",
    "encrypt_note",
    "list_encrypted",
]
