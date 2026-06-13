"""Email validation + typo suggestion.

Two jobs:
  * ``check_email`` — normalise, validate the shape, and suggest a fix for
    common domain typos (``gmail.ru`` → ``gmail.com``, ``gmial.com`` → …).

Used by the magic-link flow (server-side hard validation) and surfaced to
the client for inline "вы имели в виду …?" hints. We intentionally do NOT
do MX/DNS lookups here — that's slow and unreliable; shape + known-typo
correction covers the overwhelming majority of real mistakes.
"""

from __future__ import annotations

import re
from typing import TypedDict

# Pragmatic email shape check (not full RFC 5322 — that's a footgun).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# Common domain typos → the domain the user almost certainly meant.
# Kept conservative: only entries that are unambiguous mistakes, never a
# real alternative domain (e.g. we do NOT remap ``mail.com`` — it exists).
_DOMAIN_TYPOS: dict[str, str] = {
    "gmail.ru": "gmail.com",
    "gmail.con": "gmail.com",
    "gmail.co": "gmail.com",
    "gmail.cm": "gmail.com",
    "gmail.om": "gmail.com",
    "gmial.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gmaill.com": "gmail.com",
    "gnail.com": "gmail.com",
    "yandex.com": "yandex.ru",
    "yandx.ru": "yandex.ru",
    "yndex.ru": "yandex.ru",
    "mail.ru.com": "mail.ru",
    "mai.ru": "mail.ru",
    "outlook.con": "outlook.com",
    "hotmail.con": "hotmail.com",
    "hotmial.com": "hotmail.com",
    "icloud.con": "icloud.com",
    "icloud.co": "icloud.com",
}


class EmailCheck(TypedDict):
    email: str           # normalised (lowercased, trimmed)
    valid: bool          # passes the shape check
    suggestion: str | None  # corrected address if a typo was detected


def check_email(raw: str | None) -> EmailCheck:
    email = (raw or "").strip().lower()
    valid = bool(_EMAIL_RE.match(email))
    suggestion: str | None = None
    if "@" in email:
        local, _, domain = email.rpartition("@")
        fixed = _DOMAIN_TYPOS.get(domain)
        if fixed and local:
            suggestion = f"{local}@{fixed}"
    return {"email": email, "valid": valid, "suggestion": suggestion}
