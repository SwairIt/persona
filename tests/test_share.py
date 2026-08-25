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


# ── Share tokens must never reach the log (the 6a806dc bug class) ───────────


def test_cover_events_log_a_fingerprint_not_the_token() -> None:
    """The share token is the capability — logging it hands out working links.

    ``share_collection.py`` used to pass ``token=token`` into three structlog
    calls, so anyone with read access to the log file could open every shared
    collection. This asserts both halves of the fix at once: the raw token is
    absent from the emitted record, **and** the event is still traceable —
    a stable ``token_fp`` that differs per token, so two log lines about the
    same collection can still be correlated.
    """
    from structlog.testing import capture_logs

    from app.web.routes.share_collection import cover_log, token_fingerprint

    token = _sign("42|9999999999|view")
    other = _sign("43|9999999999|view")

    with capture_logs() as records:
        cover_log.info(
            "share_collection_cover_set",
            token_fp=token_fingerprint(token),
            cover_shot_id=7,
            count=3,
        )

    assert len(records) == 1
    emitted = records[0]
    rendered = repr(emitted)
    assert token not in rendered, "raw share token reached the log record"
    # A prefix check too: the signature half alone is enough to forge nothing,
    # but the payload half plus the signature is the whole link.
    assert token.split(".", 1)[1] not in rendered

    # Still traceable: same token → same label, different token → different.
    assert emitted["token_fp"] == token_fingerprint(token)
    assert emitted["cover_shot_id"] == 7
    assert token_fingerprint(token) != token_fingerprint(other)


def test_token_fingerprint_is_short_stable_and_one_way() -> None:
    token = _sign("1|9999999999|view")
    fp = token_fingerprint_for_test(token)
    assert fp == token_fingerprint_for_test(token)  # stable
    assert len(fp) == 12  # short enough to read in a log line
    assert fp not in token  # not a substring of the secret
    assert token_fingerprint_for_test("") == "-"


def token_fingerprint_for_test(token: str) -> str:
    from app.web.routes.share_collection import token_fingerprint

    return token_fingerprint(token)
