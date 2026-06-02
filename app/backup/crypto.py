"""Encryption helpers built on the stdlib + `cryptography` (already a dep of others).

We deliberately use a *very* simple AES-256-GCM format that anyone can audit:

    magic      = b"PERSONABACK\\x01"   (12 bytes)
    salt       = 16 random bytes
    iterations = 4-byte big-endian uint (PBKDF2 iterations)
    nonce      = 12 random bytes
    ciphertext (variable) — encrypts the original ZIP bytes
    tag        = 16-byte GCM auth tag (suffix of ciphertext)

Passphrase → PBKDF2-HMAC-SHA256 → 32-byte AES key.

No external CLI required to decrypt; included Python script can do it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from dataclasses import dataclass

MAGIC = b"PERSONABACK\x01"
DEFAULT_ITERATIONS = 600_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32


class CryptoError(RuntimeError):
    """Raised on any encrypt/decrypt failure."""


def _check_cryptography_available() -> None:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError as exc:
        msg = (
            "Encrypted backup requires the `cryptography` package. "
            "Add it to your environment via `uv add cryptography`."
        )
        raise CryptoError(msg) from exc


@dataclass(frozen=True, slots=True)
class EncryptedEnvelope:
    salt: bytes
    iterations: int
    nonce: bytes
    ciphertext: bytes


def derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if iterations < 50_000:
        msg = "PBKDF2 iterations too low"
        raise CryptoError(msg)
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        iterations,
        dklen=KEY_BYTES,
    )


def encrypt(payload: bytes, passphrase: str, *, iterations: int = DEFAULT_ITERATIONS) -> bytes:
    """Encrypt `payload` using AES-256-GCM derived from `passphrase`."""
    _check_cryptography_available()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    key = derive_key(passphrase, salt, iterations)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, payload, MAGIC)

    out = (
        MAGIC
        + salt
        + struct.pack(">I", iterations)
        + nonce
        + ciphertext
    )
    return out


def decrypt(blob: bytes, passphrase: str) -> bytes:
    """Decrypt a blob produced by `encrypt`. Raises CryptoError on bad pass."""
    _check_cryptography_available()
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(MAGIC):
        msg = "Not a Persona encrypted backup (magic header mismatch)"
        raise CryptoError(msg)

    offset = len(MAGIC)
    salt = blob[offset : offset + SALT_BYTES]
    offset += SALT_BYTES
    (iterations,) = struct.unpack(">I", blob[offset : offset + 4])
    offset += 4
    nonce = blob[offset : offset + NONCE_BYTES]
    offset += NONCE_BYTES
    ciphertext = blob[offset:]

    key = derive_key(passphrase, salt, iterations)
    aesgcm = AESGCM(key)
    try:
        return bytes(aesgcm.decrypt(nonce, ciphertext, MAGIC))
    except InvalidTag as exc:
        msg = "Wrong passphrase, or backup file is corrupted"
        raise CryptoError(msg) from exc


def fingerprint(passphrase: str) -> str:
    """Stable 16-char fingerprint of a passphrase — display-only safety check."""
    digest = hmac.new(b"persona-fp", passphrase.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:16]
