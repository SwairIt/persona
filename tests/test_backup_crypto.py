"""Tests for the encryption envelope. Skipped if `cryptography` is missing."""

from __future__ import annotations

import pytest

cryptography = pytest.importorskip("cryptography")

from app.backup.crypto import (
    CryptoError,
    decrypt,
    encrypt,
    fingerprint,
)


def test_roundtrip_short_payload() -> None:
    payload = b"hello persona"
    blob = encrypt(payload, "a very long passphrase for tests")
    assert blob.startswith(b"PERSONABACK")
    out = decrypt(blob, "a very long passphrase for tests")
    assert out == payload


def test_roundtrip_large_payload() -> None:
    payload = b"x" * 50_000
    blob = encrypt(payload, "another long passphrase here")
    assert decrypt(blob, "another long passphrase here") == payload


def test_wrong_passphrase_fails() -> None:
    blob = encrypt(b"secret", "correct passphrase value")
    with pytest.raises(CryptoError):
        decrypt(blob, "wrong passphrase value")


def test_invalid_magic_fails() -> None:
    with pytest.raises(CryptoError):
        decrypt(b"garbage", "anything")


def test_fingerprint_stable_and_short() -> None:
    a = fingerprint("hunter22hunter22")
    b = fingerprint("hunter22hunter22")
    c = fingerprint("different one here")
    assert a == b
    assert a != c
    assert len(a) == 16
