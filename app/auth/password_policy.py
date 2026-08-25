"""Password strength floor — length + an embedded worst-passwords blocklist.

Design constraints
------------------
* **No new dependency.** ``zxcvbn`` / ``have-i-been-pwned`` lookups are out;
  the blocklist below is embedded and costs ~4 KB of source.
* **Floor, not a nag.** The goal is to stop the passwords that a credential
  stuffer tries in the first thousand guesses, not to enforce character-class
  theatre ("must contain an uppercase and a symbol") which mostly produces
  ``Password1!`` and is explicitly discouraged by NIST SP 800-63B §5.1.1.2.
* **Fail closed.** Anything the checker cannot evaluate is rejected, never
  silently accepted.

Rules applied, in order:

1. length ≥ :data:`MIN_LENGTH` (8 — unchanged from the previous policy, so no
   existing account or flow becomes un-loginable);
2. length ≤ :data:`MAX_LENGTH` (1024) — a 10 MB "password" is a PBKDF2 CPU
   denial-of-service, since hashing cost scales with input handling;
3. the normalised password (lowercased, surrounding whitespace stripped) is not
   in :data:`WORST_PASSWORDS`;
4. the password is not a *trivially decorated* blocklist entry: trailing digits
   and a trailing ``!`` are stripped before the blocklist test, so ``qwerty123``
   and ``password1!`` are caught too;
5. the password is not a single repeated character (``aaaaaaaa``) and not a
   straight run off the keyboard/alphabet/digits (``12345678``, ``abcdefgh``);
6. the password *is not* the local part of the account's own email (checked
   when the caller passes ``email=``) — ``ivan@mail.ru`` / ``ivan12345`` is the
   single most guessable shape there is. Deliberately narrow: a plain
   "local part appears anywhere in the password" test rejects a large share of
   ordinary passphrases (``owner-pass-123`` for ``owner@…``), which is friction
   without a matching security gain. See :func:`_check_not_the_email`.

Errors are raised as ``ValueError`` with a *stable English key* so the existing
route-level translation table in ``app/web/routes/auth.py`` keeps working; the
RU strings live next to the other user-facing copy.
"""

from __future__ import annotations

import re

__all__ = [
    "MIN_LENGTH",
    "MAX_LENGTH",
    "WORST_PASSWORDS",
    "ERR_TOO_SHORT",
    "ERR_TOO_LONG",
    "ERR_COMMON",
    "ERR_SEQUENTIAL",
    "ERR_CONTAINS_EMAIL",
    "check_password",
]

MIN_LENGTH = 8
MAX_LENGTH = 1024

# Stable error keys. Kept ASCII so they survive any logging/JSON path.
ERR_TOO_SHORT = f"password must be at least {MIN_LENGTH} characters"
ERR_TOO_LONG = f"password must be at most {MAX_LENGTH} characters"
ERR_COMMON = "password is too common"
ERR_SEQUENTIAL = "password is too simple"
ERR_CONTAINS_EMAIL = "password must not contain your email"

# Top worst passwords, de-duplicated to the entries that are ≥ 8 chars OR that
# become ≥ 8 chars once a decoration suffix is stripped. Shorter entries are
# kept anyway: rule 4 strips digits, so "qwerty" catches "qwerty12".
# Sources: NCSC "top 100k breached" head, SecLists 10k-most-common head.
WORST_PASSWORDS: frozenset[str] = frozenset(
    {
        "password", "passwort", "parol", "parola", "пароль",
        "123456", "1234567", "12345678", "123456789", "1234567890",
        "12345", "1234", "111111", "000000", "121212", "123123",
        "654321", "666666", "888888", "999999", "112233", "159753",
        "qwerty", "qwertyui", "qwertyuiop", "qwerty123", "qwe123",
        "qwertz", "azerty", "asdfgh", "asdfghjk", "zxcvbn", "zxcvbnm",
        "1q2w3e", "1q2w3e4r", "1q2w3e4r5t", "q1w2e3r4", "1qaz2wsx",
        "iloveyou", "sunshine", "princess", "football", "baseball",
        "monkey", "dragon", "master", "shadow", "superman", "batman",
        "michael", "jennifer", "jordan", "hunter", "trustno1",
        "letmein", "welcome", "welcome1", "admin", "administrator",
        "root", "toor", "guest", "test", "test123", "testtest",
        "changeme", "default", "secret", "login", "passw0rd",
        "p@ssword", "p@ssw0rd", "abc123", "abcd1234", "abcdefg",
        "starwars", "whatever", "computer", "internet", "samsung",
        "google", "facebook", "myspace1", "linkedin", "photoshop",
        "freedom", "flower", "hottie", "loveme", "zaq12wsx",
        "cheese", "chocolate", "liverpool", "arsenal", "chelsea",
        "matrix", "ninja", "mustang", "harley", "ranger", "buster",
        "soccer", "hockey", "killer", "george", "andrew", "charlie",
        "thomas", "robert", "daniel", "joshua", "matthew", "anthony",
        "nicole", "ashley", "amanda", "jessica", "michelle", "hannah",
        "summer", "winter", "spring", "autumn", "orange", "purple",
        "banana", "pokemon", "minecraft", "fortnite", "roblox",
        "qazwsx", "qazwsxedc", "asdasd", "asdasdasd", "aaaaaa",
        "trustme", "nopassword", "passpass", "pass123", "pass1234",
        "iloveyou1", "loveyou", "lovely", "family", "friends",
        # RU-keyboard equivalents / transliterations seen in RU breaches.
        "ghbdtn", "ghbdtn123", "privet", "privet123", "yfnfif",
        "solnyshko", "natasha", "nastya", "sergey", "aleksandr",
        "alexander", "vladimir", "dmitriy", "andrey", "maksim",
        "marina", "svetlana", "ekaterina", "kristina",
        "zxcvbnm123", "qwerty12", "qwerty1", "123qwe", "123qweasd",
        "spartak", "zenit", "dynamo", "rossiya", "moskva", "putin",
    }
)

# Trailing decoration a human adds to satisfy a "must contain a digit" rule.
_DECORATION_RE = re.compile(r"[0-9!@#$%^&*_.\-]+$")

# Sequential runs we treat as "not a password" regardless of length.
_KEYBOARD_ROWS = (
    "1234567890",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "abcdefghijklmnopqrstuvwxyz",
    "йцукенгшщзхъ",
    "фывапролджэ",
    "ячсмитьбю",
)


def _strip_decoration(value: str) -> str:
    """Remove one trailing run of digits/punctuation (``qwerty123`` → ``qwerty``)."""
    stripped = _DECORATION_RE.sub("", value)
    return stripped or value


def _is_run(value: str) -> bool:
    """True when ``value`` is a straight run along a keyboard row or alphabet.

    Both directions are checked, so ``987654321`` and ``poiuytre`` count.
    """
    if len(value) < 4:
        return False
    for row in _KEYBOARD_ROWS:
        if value in row or value in row[::-1]:
            return True
    return False


def check_password(password: str, *, email: str | None = None) -> None:
    """Raise ``ValueError`` when ``password`` is below the strength floor.

    ``email`` is optional; when given, the local part (before ``@``) must not
    appear inside the password. Passing it is strongly preferred at signup.
    """
    if not isinstance(password, str) or not password:
        raise ValueError(ERR_TOO_SHORT)
    if len(password) < MIN_LENGTH:
        raise ValueError(ERR_TOO_SHORT)
    if len(password) > MAX_LENGTH:
        raise ValueError(ERR_TOO_LONG)

    lowered = password.strip().lower()

    if lowered in WORST_PASSWORDS:
        raise ValueError(ERR_COMMON)
    undecorated = _strip_decoration(lowered)
    if undecorated in WORST_PASSWORDS:
        raise ValueError(ERR_COMMON)

    # A single repeated character, or a straight keyboard/alphabet run.
    if len(set(lowered)) == 1:
        raise ValueError(ERR_SEQUENTIAL)
    if _is_run(lowered) or _is_run(undecorated):
        raise ValueError(ERR_SEQUENTIAL)

    if email:
        _check_not_the_email(lowered, undecorated, str(email))
    return None


# How much of the password the email local part may occupy before the password
# is "just my username". 0.6 was picked so that ``ivan1234`` (local ``ivan``,
# 4/8 = 0.5 but undecorated == local → caught by the equality rule) and
# ``ivanov1234`` are rejected, while ``owner-pass-123`` / ``mynamepass123`` —
# passwords that merely *start* with a name — are not. A plain substring test
# was tried first and turned out to reject a large share of ordinary
# passphrases; the friction was not worth the marginal guessing advantage.
_EMAIL_SHARE_LIMIT = 0.6


def _check_not_the_email(lowered: str, undecorated: str, email: str) -> None:
    """Reject a password that *is* the account's email local part."""
    local = email.strip().lower().partition("@")[0]
    # 3 chars is the shortest local part worth matching; below that the
    # false-positive rate ("an@…" inside any word) outweighs the value.
    if len(local) < 3:
        return
    if lowered == local or undecorated == local:
        raise ValueError(ERR_CONTAINS_EMAIL)
    if local in lowered and len(local) / len(lowered) >= _EMAIL_SHARE_LIMIT:
        raise ValueError(ERR_CONTAINS_EMAIL)
