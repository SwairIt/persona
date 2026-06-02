"""Redact obviously-sensitive text patterns from OCR output before storage."""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,16}\d\b")
_PHONE_RE = re.compile(
    r"\+?\d{1,3}[\s\-]?\(?\d{2,4}\)?[\s\-]?\d{2,4}[\s\-]?\d{2,4}[\s\-]?\d{0,4}"
)
_API_KEY_RE = re.compile(r"\b(?:sk-(?:ant|proj|live|test)[\w-]{20,}|gsk_[\w]{20,}|xoxb-[\w-]{20,})\b")
_SECRET_LINE_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*\S+"
)


def redact(text: str | None) -> str | None:
    """Replace obvious secrets with [redacted]. Idempotent."""
    if not text:
        return text
    out = text
    out = _API_KEY_RE.sub("[redacted-api-key]", out)
    out = _SECRET_LINE_RE.sub("[redacted-secret]", out)
    out = _CARD_RE.sub("[redacted-card]", out)
    out = _IBAN_RE.sub("[redacted-iban]", out)
    out = _EMAIL_RE.sub("[redacted-email]", out)
    out = _PHONE_RE.sub("[redacted-phone]", out)
    return out
