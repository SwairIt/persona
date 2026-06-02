"""Adaptive capture cadence — grows on idle, shrinks on activity."""

from __future__ import annotations


def compute_interval(
    base_seconds: float,
    idle_seconds: float,
    min_s: float,
    max_s: float,
) -> float:
    """Return the next capture interval based on user idle time.

    Algorithm:
      * ``idle_seconds < 30``      -> ``min_s`` (user is actively working,
        capture fast).
      * ``idle_seconds < 120``     -> ``base_seconds`` (regular cadence).
      * otherwise                  -> ``min(max_s, base_seconds *
        (1 + idle_seconds / 300.0))`` — interval grows linearly with idle
        time and is clamped to ``max_s``.

    The result is additionally clamped to the closed interval
    ``[min_s, max_s]`` so callers get a usable value even if
    ``base_seconds`` itself sits outside the bounds.
    """
    if max_s < min_s:
        msg = f"max_s ({max_s}) must be >= min_s ({min_s})"
        raise ValueError(msg)

    if idle_seconds < 30:
        interval = min_s
    elif idle_seconds < 120:
        interval = base_seconds
    else:
        interval = min(max_s, base_seconds * (1.0 + idle_seconds / 300.0))

    if interval < min_s:
        return min_s
    if interval > max_s:
        return max_s
    return interval
