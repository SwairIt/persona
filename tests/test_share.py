"""Tests for share-link signing + verification."""

from __future__ import annotations

import time

from app.web.routes.share import _sign, _verify, create_share_token


def test_sign_verify_roundtrip() -> None:
    payload = "42|9999999999|view"
    token = _sign(payload)
    parsed = _verify(token)
    assert parsed is not None
    assert parsed["screenshot_id"] == 42
    assert parsed["purpose"] == "view"


def test_verify_rejects_tampered_token() -> None:
    token = _sign("99|9999999999|view")
    encoded, sig = token.split(".", 1)
    bad = encoded + "." + "0" * len(sig)
    assert _verify(bad) is None


def test_verify_rejects_expired() -> None:
    expires = int(time.time()) - 100
    token = _sign(f"7|{expires}|view")
    assert _verify(token) is None


def test_create_share_token_for_existing_id() -> None:
    token = create_share_token(123, ttl_hours=1)
    parsed = _verify(token)
    assert parsed is not None
    assert parsed["screenshot_id"] == 123
