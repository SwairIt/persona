"""Encrypted key/value vault — BYO LLM keys, webhook secrets, SMTP passwords.

The user supplies a single master password. Each secret is wrapped with
its own Fernet key, derived from ``PBKDF2-HMAC-SHA256(password + key,
salt, 100_000)`` over a fresh per-row 16-byte salt. The salt is
prepended to the Fernet ciphertext and the whole envelope is what we
hand to SQLite — so a row can be decrypted in isolation, and rotating
one secret never forces a rewrite of the others.

The ``cryptography`` package is an *optional* runtime dependency: the
import is wrapped in ``try`` and every public mutator returns
``{"status": "missing_dep"}`` instead of crashing when it's not on the
system. This mirrors the SMTP / backup modules, which already do the
same dance for ``aiosmtplib`` and ``cryptography`` respectively.

Logging is structured (``persona.vault``) and **never** carries the
plaintext value or password — we only log keys, status, and counts.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.vault")

# Per-row salt is generated fresh on every ``set_secret`` call. 16 bytes
# is the standard PBKDF2 recommendation; longer buys nothing here.
_SALT_BYTES = 16
# PBKDF2 work factor. Matches the task spec (100k); the audited backup
# crypto in :mod:`app.backup.crypto` uses 600k, but Fernet's own key
# size + per-row salt keep the brute-force budget reasonable.
_KDF_ITERATIONS = 100_000
# Fernet expects exactly 32 bytes (base64-urlsafe-encoded) as the key.
_FERNET_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# Optional-dep probe — keeps the import side-effect free
# ---------------------------------------------------------------------------


def _cryptography_available() -> bool:
    """Return True iff the ``cryptography`` package can be imported."""
    try:
        import cryptography.fernet  # noqa: F401, PLC0415 — optional dep probe
    except ImportError:
        return False
    return True


_MISSING_DEP_HINT = (
    "cryptography is required for the encrypted vault. "
    "Install it with `uv pip install cryptography` and restart Persona."
)


def _derive_fernet_key(password: str, key: str, salt: bytes) -> bytes:
    """Derive a 32-byte base64-urlsafe Fernet key from (password + key + salt).

    The KDF input is the literal concatenation of the password and the
    row key — binding the derivation to the key name means a leaked
    ciphertext for one row cannot be decrypted by a different (also
    leaked) row's derivation, even under the same master password.
    """
    import base64  # noqa: PLC0415 — keep the module-level import surface tiny

    raw = hashlib.pbkdf2_hmac(
        "sha256",
        (password + key).encode("utf-8"),
        salt,
        _KDF_ITERATIONS,
        dklen=_FERNET_KEY_BYTES,
    )
    return base64.urlsafe_b64encode(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def set_secret(key: str, value: str, password: str) -> dict[str, Any]:
    """Encrypt ``value`` under ``password`` and store the envelope.

    Returns a status dict: ``{"status": "ok"}`` on success,
    ``{"status": "missing_dep", ...}`` if ``cryptography`` is not
    installed, or ``{"status": "invalid", ...}`` if inputs are empty.

    Existing rows for the same ``key`` are overwritten (``INSERT … ON
    CONFLICT DO UPDATE``) so rotating a secret is a single call.
    """
    if not key or not password:
        log.warning("vault.set.invalid", key=key, has_password=bool(password))
        return {"status": "invalid", "error": "key and password are required"}

    if not _cryptography_available():
        log.warning("vault.set.missing_dep", key=key)
        return {"status": "missing_dep", "hint": _MISSING_DEP_HINT}

    from cryptography.fernet import Fernet  # noqa: PLC0415 — optional dep

    salt = os.urandom(_SALT_BYTES)
    fernet_key = _derive_fernet_key(password, key, salt)
    token = Fernet(fernet_key).encrypt(value.encode("utf-8"))
    envelope = salt + token

    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO kv_vault (key, ciphertext)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                ciphertext = excluded.ciphertext,
                created_at = datetime('now')
            """,
            (key, envelope),
        )
        await conn.commit()

    log.info("vault.set.ok", key=key, bytes=len(envelope))
    return {"status": "ok"}


async def get_secret(key: str, password: str) -> dict[str, Any]:
    """Decrypt the stored secret for ``key`` using ``password``.

    Returns one of:
        * ``{"status": "ok", "value": "<plaintext>"}``
        * ``{"status": "missing_dep", ...}``
        * ``{"status": "not_found"}``
        * ``{"status": "wrong_password"}``

    The plaintext ``value`` is **never** logged. Callers must treat the
    returned dict as sensitive and avoid putting it through any logger
    that might persist payloads.
    """
    if not key or not password:
        return {"status": "invalid", "error": "key and password are required"}

    if not _cryptography_available():
        log.warning("vault.get.missing_dep", key=key)
        return {"status": "missing_dep", "hint": _MISSING_DEP_HINT}

    from cryptography.fernet import Fernet, InvalidToken  # noqa: PLC0415

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ciphertext FROM kv_vault WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()

    if row is None:
        log.info("vault.get.not_found", key=key)
        return {"status": "not_found"}

    envelope = bytes(row["ciphertext"])
    if len(envelope) <= _SALT_BYTES:
        # Corrupt row — treat as a decryption failure rather than 500.
        log.warning("vault.get.corrupt", key=key, bytes=len(envelope))
        return {"status": "wrong_password"}

    salt = envelope[:_SALT_BYTES]
    token = envelope[_SALT_BYTES:]
    fernet_key = _derive_fernet_key(password, key, salt)
    try:
        plaintext = Fernet(fernet_key).decrypt(token)
    except InvalidToken:
        log.info("vault.get.wrong_password", key=key)
        return {"status": "wrong_password"}

    log.info("vault.get.ok", key=key)
    return {"status": "ok", "value": plaintext.decode("utf-8")}


async def list_keys() -> list[dict[str, str]]:
    """Return every stored key (name + creation timestamp) — never the value."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT key, created_at FROM kv_vault ORDER BY key ASC",
        )
        rows: list[aiosqlite.Row] = list(await cursor.fetchall())
    items = [{"key": str(row["key"]), "created_at": str(row["created_at"])} for row in rows]
    log.info("vault.list", count=len(items))
    return items


async def delete_secret(key: str) -> dict[str, Any]:
    """Drop the row for ``key`` regardless of password.

    Deletion does *not* require the master password — losing the
    password should not lock you out of *removing* an entry. The trade
    off is intentional: an attacker with DB write access can already
    drop the row directly, so gating the route adds no real security.
    """
    if not key:
        return {"status": "invalid", "error": "key is required"}

    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM kv_vault WHERE key = ?",
            (key,),
        )
        await conn.commit()
        deleted = cursor.rowcount

    log.info("vault.delete", key=key, deleted=deleted)
    return {"status": "ok" if deleted else "not_found", "deleted": int(deleted)}
