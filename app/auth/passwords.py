"""Password hashing using stdlib PBKDF2-HMAC-SHA256.

No external dependency (bcrypt / argon2) — keeps the install lean. The
OWASP 2023 recommendation for PBKDF2-SHA256 is 600,000 iterations, which
takes ~250 ms on a modern CPU. That's slow enough to make brute force
expensive but fast enough that a login round-trip stays under 500 ms.

Hash record format:
    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

Storing the iteration count inside the record lets us bump the cost
factor over time without invalidating old rows — verify_password reads
the stored iterations, not a global constant.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGO_PREFIX = "pbkdf2_sha256"
_ITERATIONS = 600_000  # OWASP 2023 minimum for PBKDF2-SHA256
_SALT_BYTES = 16
_HASH_BYTES = 32  # 256 bits — same as SHA-256 output


def hash_password(plain: str) -> str:
    """Return a self-describing PBKDF2 hash record for ``plain``."""
    if not plain:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, _ITERATIONS, dklen=_HASH_BYTES
    )
    return f"{_ALGO_PREFIX}${_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """Constant-time compare ``plain`` against a stored hash record."""
    if not plain or not stored:
        return False
    try:
        algo, iterations_str, salt_hex, hash_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algo != _ALGO_PREFIX:
        return False
    try:
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if iterations < 1 or len(salt) < 8 or len(expected) < 16:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, iterations, dklen=len(expected)
    )
    return hmac.compare_digest(derived, expected)
