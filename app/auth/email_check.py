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
    "gmail.con": "gmail.com",
    "gmail.cm": "gmail.com",
    "gmail.om": "gmail.com",
    "gmial.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gmaill.com": "gmail.com",
    "gnail.com": "gmail.com",
    "gmail.ocm": "gmail.com",
    "yandex.com": "yandex.ru",
    "yandx.ru": "yandex.ru",
    "yndex.ru": "yandex.ru",
    "yadex.ru": "yandex.ru",
    "mail.ru.com": "mail.ru",
    "mai.ru": "mail.ru",
    "mial.ru": "mail.ru",
    "outlook.con": "outlook.com",
    "outloo.com": "outlook.com",
    "hotmail.con": "hotmail.com",
    "hotmial.com": "hotmail.com",
    "hotmai.com": "hotmail.com",
    "icloud.con": "icloud.com",
    "icloud.co": "icloud.com",
}

# Провайдеры с ЕДИНСТВЕННЫМ правильным доменом: любой другой их вариант —
# опечатка (gmail.ru / gmail.co / gmail.xyz → gmail.com). Только домены без
# легитимных страновых вариантов, чтобы не ловить ложных срабатываний
# (поэтому тут НЕТ hotmail/outlook — у них есть hotmail.co.uk, outlook.fr и т.п.).
_CANONICAL_PROVIDERS: dict[str, str] = {
    "gmail": "gmail.com",
    "googlemail": "gmail.com",
    "icloud": "icloud.com",
    "protonmail": "proton.me",
}


class EmailCheck(TypedDict):
    email: str           # normalised (lowercased, trimmed)
    valid: bool          # passes the shape check
    suggestion: str | None  # corrected address if a typo was detected


def _suggest_domain(domain: str) -> str | None:
    """Подсказать правильный домен для распознанной опечатки, иначе None."""
    if domain in _DOMAIN_TYPOS:
        return _DOMAIN_TYPOS[domain]
    sld = domain.split(".", 1)[0]
    canon = _CANONICAL_PROVIDERS.get(sld)
    if canon and domain != canon:
        return canon
    return None


def check_email(raw: str | None) -> EmailCheck:
    email = (raw or "").strip().lower()
    valid = bool(_EMAIL_RE.match(email))
    suggestion: str | None = None
    if "@" in email:
        local, _, domain = email.rpartition("@")
        if local:
            fixed = _suggest_domain(domain)
            if fixed:
                suggestion = f"{local}@{fixed}"
    return {"email": email, "valid": valid, "suggestion": suggestion}
