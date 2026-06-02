"""Tests for sensitive-content redaction."""

from __future__ import annotations

from app.ocr.redaction import redact


def test_redact_none_or_empty() -> None:
    assert redact(None) is None
    assert redact("") == ""


def test_redact_email() -> None:
    assert "[redacted-email]" in redact("contact: user@example.com please")


def test_redact_card_number() -> None:
    text = "card 4111 1111 1111 1111 expires 12/29"
    out = redact(text) or ""
    assert "[redacted-card]" in out
    assert "4111" not in out


def test_redact_iban() -> None:
    assert "[redacted-iban]" in (redact("IBAN: DE89370400440532013000") or "")


def test_redact_api_key() -> None:
    out = redact("export ANTHROPIC=sk-ant-api03-aaaabbbbccccdddd0011223344") or ""
    assert "sk-ant-api03" not in out


def test_redact_password_line() -> None:
    out = redact("password: hunter2hunter2") or ""
    assert "[redacted-secret]" in out
    assert "hunter2" not in out


def test_redact_keeps_normal_text() -> None:
    text = "Meeting at 14:30 about migration progress"
    assert redact(text) == text


def test_redact_is_idempotent() -> None:
    text = "email me at a@b.com"
    once = redact(text)
    twice = redact(once)
    assert once == twice
