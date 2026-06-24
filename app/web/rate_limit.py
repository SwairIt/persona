"""In-process sliding-window rate limiter (dependency-free).

A single :func:`allow` predicate backed by a module-level
``dict[str, deque[float]]`` of event timestamps taken from
:func:`time.monotonic` (immune to wall-clock jumps / NTP steps). On each call
we drop timestamps that have aged out of the window, then either reject (the
count would exceed ``max_events``) or record ``now`` and accept.

Scope & threading
-----------------
This limiter holds no cross-process state — with ``uvicorn --workers N`` each
worker keeps its own counters, so the effective global ceiling is
``N x max_events``. That is acceptable for the coarse abuse limits it guards
(login attempts, mint endpoints); a precise global limit would need Redis.
A :class:`threading.Lock` guards the dict so it is safe under a single async
event loop and under a threadpool (:func:`asyncio.to_thread`).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

__all__ = ["allow"]

# key -> monotonic timestamps of accepted events, oldest first.
_EVENTS: dict[str, deque[float]] = defaultdict(deque)
_LOCK = threading.Lock()


def allow(key: str, max_events: int, window_seconds: int) -> bool:
    """Return True if an event under ``key`` is allowed right now.

    Sliding window: at most ``max_events`` events may occur within any
    ``window_seconds`` interval. When allowed, the event is recorded (``now``
    appended) before returning ``True``. When the window is already full the
    function returns ``False`` WITHOUT recording, so a rejected caller does
    not push the window forward.

    A non-positive ``max_events`` blocks everything (returns ``False``).
    """
    if max_events <= 0:
        return False
    now = time.monotonic()
    cutoff = now - float(window_seconds)
    with _LOCK:
        bucket = _EVENTS[key]
        # Evict timestamps that have slid out of the window.
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= max_events:
            # Bucket empty after eviction is impossible here (len >= max >= 1),
            # so nothing to clean up; just refuse.
            return False
        if not bucket:
            # Drop stale empty buckets opportunistically to bound memory for
            # one-shot keys, then start fresh.
            _EVENTS.pop(key, None)
            bucket = _EVENTS[key]
        bucket.append(now)
        return True


if __name__ == "__main__":  # pragma: no cover — tiny self-check
    import sys

    # 3 events allowed in a 10s window; the 4th is refused.
    assert allow("a", 3, 10) is True
    assert allow("a", 3, 10) is True
    assert allow("a", 3, 10) is True
    assert allow("a", 3, 10) is False, "4th event in window must be refused"

    # Keys are independent.
    assert allow("b", 1, 10) is True
    assert allow("b", 1, 10) is False

    # A zero-width window never blocks: every prior event is already outside.
    assert allow("c", 1, 0) is True
    assert allow("c", 1, 0) is True

    # Non-positive max blocks everything.
    assert allow("d", 0, 10) is False

    print("rate_limit self-check OK")
    sys.exit(0)
