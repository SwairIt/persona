"""Per-ACCOUNT login throttling with exponential backoff.

Why per-account and not only per-IP
-----------------------------------
``app/web/routes/auth.py`` already limits login attempts per client IP
(20 / hour). That stops one box hammering the whole login endpoint, but it is
close to free for an attacker to rotate source IPs — residential proxy pools
are sold by the gigabyte. A credential-stuffing run against **one** known email
address (say, the owner's, which is discoverable) costs the attacker nothing
under a purely per-IP scheme: 20 tries from each of a thousand IPs is 20 000
guesses against that account.

So the counter also lives on the **account**, where the attacker cannot rotate
it away.

Policy
------
Failures are counted per normalised email in a sliding window
(:data:`WINDOW_SECONDS`, 1 h). Once :data:`FREE_ATTEMPTS` (5) consecutive
failures are recorded, each further failure arms a lockout whose length doubles:

    6th failure → 30 s, 7th → 60 s, 8th → 2 min, 9th → 4 min, … capped at
    :data:`MAX_LOCK_SECONDS` (15 min).

A **successful** login clears the account's record immediately, so a user who
mistypes twice and then gets it right is never penalised.

Deliberate design choices
-------------------------
* **In-process, like** :mod:`app.web.rate_limit`. No table, no migration, no
  cleanup job. With ``uvicorn --workers N`` each worker keeps its own counter,
  so the effective ceiling is N× — the same caveat the existing limiter
  documents and accepts. It is a speed bump against automation, and a speed
  bump that resets on deploy is still worth having; a precise global counter
  needs shared state Persona does not have.
* **Never enumerates.** :func:`locked_for` is consulted *before* the password
  is checked, and the caller returns the same generic 429 it returns for the
  per-IP limit. An attacker learns "this address is rate limited", which is
  true for any address they have guessed at, existing or not — the counter is
  keyed on whatever string was submitted, so a non-existent address locks out
  exactly like a real one.
* **Fails safe.** Any internal error resolves to "locked" rather than "open";
  see :func:`locked_for`.
* **Bounded memory.** At most :data:`_MAX_TRACKED` accounts are tracked; the
  oldest record is evicted first. An attacker spraying millions of distinct
  addresses evicts *their own* records, not a real user's protection, because
  eviction is by last-touch time and a real user under attack is being touched
  constantly.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.logging_setup import get_logger

log = get_logger("persona.auth.lockout")

__all__ = [
    "FREE_ATTEMPTS",
    "MAX_LOCK_SECONDS",
    "WINDOW_SECONDS",
    "clear",
    "locked_for",
    "record_failure",
    "reset_all",
]

FREE_ATTEMPTS = 5
BASE_LOCK_SECONDS = 30.0
MAX_LOCK_SECONDS = 900.0  # 15 min
WINDOW_SECONDS = 3600.0  # failures older than this stop counting
_MAX_TRACKED = 4096


@dataclass
class _Record:
    failures: int = 0
    first_failure_at: float = 0.0
    locked_until: float = 0.0
    touched_at: float = field(default_factory=time.monotonic)


_records: dict[str, _Record] = {}
_lock = threading.Lock()


def _key(email: str) -> str:
    """Normalise the submitted identifier. Never raises on junk input."""
    return (email or "").strip().lower()[:254]


def _evict_locked() -> None:
    """Drop the least-recently-touched records. Caller holds ``_lock``."""
    if len(_records) <= _MAX_TRACKED:
        return
    victims = sorted(_records.items(), key=lambda kv: kv[1].touched_at)
    for name, _record in victims[: len(_records) - _MAX_TRACKED]:
        _records.pop(name, None)


def locked_for(email: str) -> float:
    """Seconds this account must wait, or ``0.0`` when it may try now.

    Fail-safe: an unexpected internal error returns :data:`BASE_LOCK_SECONDS`
    (deny) rather than 0 (allow). A login being briefly refused is recoverable;
    an unbounded guessing window is not.
    """
    try:
        key = _key(email)
        if not key:
            return 0.0
        now = time.monotonic()
        with _lock:
            record = _records.get(key)
            if record is None:
                return 0.0
            # Window expired with no lock pending → forget the account.
            if (
                record.locked_until <= now
                and now - record.first_failure_at > WINDOW_SECONDS
            ):
                _records.pop(key, None)
                return 0.0
            remaining = record.locked_until - now
            return remaining if remaining > 0 else 0.0
    except Exception as exc:  # noqa: BLE001 — deny on error, never open
        log.warning("auth.lockout.check_failed", error=str(exc))
        return BASE_LOCK_SECONDS


def record_failure(email: str) -> float:
    """Record one failed attempt. Returns the lock duration now in force (s)."""
    try:
        key = _key(email)
        if not key:
            return 0.0
        now = time.monotonic()
        with _lock:
            record = _records.get(key)
            if record is None or now - record.first_failure_at > WINDOW_SECONDS:
                record = _Record(failures=0, first_failure_at=now)
                _records[key] = record
            record.failures += 1
            record.touched_at = now
            over = record.failures - FREE_ATTEMPTS
            if over <= 0:
                _evict_locked()
                return 0.0
            # 30 s, 60 s, 120 s, … capped.
            delay = min(BASE_LOCK_SECONDS * (2 ** (over - 1)), MAX_LOCK_SECONDS)
            record.locked_until = now + delay
            _evict_locked()
        log.warning(
            "auth.lockout.armed",
            # The address itself is not logged — only its domain, so the log is
            # useful for spotting a targeted campaign without becoming a list of
            # user emails.
            email_domain=key.rpartition("@")[2] or "?",
            failures=record.failures,
            lock_seconds=int(delay),
        )
        return delay
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not break login
        log.warning("auth.lockout.record_failed", error=str(exc))
        return 0.0


def clear(email: str) -> None:
    """Forget an account's failures — called on a successful authentication."""
    try:
        with _lock:
            _records.pop(_key(email), None)
    except Exception as exc:  # noqa: BLE001
        log.debug("auth.lockout.clear_failed", error=str(exc))


def reset_all() -> None:
    """Wipe every record (tests, and an owner-triggered 'unlock everyone')."""
    with _lock:
        _records.clear()
