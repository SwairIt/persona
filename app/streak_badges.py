"""Streak milestone badges — celebrate 3/7/14/30/60/90/180/365-day runs.

Pure, deterministic projection of the longest-ever streak into a list of
milestone badge dicts.  The streak page renders the earned ones as filled
chips, the next unearned threshold dimmed as a teaser.

The ``earned_at_inferred`` flag is ``True`` for every returned row because
we don't actually persist *when* a milestone was first crossed — we only
know the user has hit at least that threshold at some point.  The flag is
exposed so the template can be honest about that (no fake dates), and so
future code can swap in real timestamps without changing the shape.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger

log = get_logger("persona.streak.badges")


class StreakBadge(TypedDict):
    threshold: int
    label: str
    earned_at_inferred: bool


STREAK_THRESHOLDS: tuple[int, ...] = (3, 7, 14, 30, 60, 90, 180, 365)


def _label_for(threshold: int) -> str:
    return f"{threshold}-day streak"


def badges_earned(longest_streak: int) -> list[StreakBadge]:
    """Return the milestone badges unlocked by ``longest_streak``.

    A badge is returned for every threshold ``t`` in :data:`STREAK_THRESHOLDS`
    such that ``t <= longest_streak``.  The list is ordered ascending by
    threshold so the template can render chips in chronological unlock order.

    Negative inputs are coerced to zero (defensive — the streak module never
    emits negatives, but the function should not blow up if a caller passes
    a sentinel).
    """
    effective = max(0, int(longest_streak))
    earned: list[StreakBadge] = [
        StreakBadge(
            threshold=threshold,
            label=_label_for(threshold),
            earned_at_inferred=True,
        )
        for threshold in STREAK_THRESHOLDS
        if threshold <= effective
    ]
    log.info(
        "streak.badges.computed",
        longest_streak=effective,
        earned_count=len(earned),
        earned_thresholds=[b["threshold"] for b in earned],
    )
    return earned


def next_threshold(longest_streak: int) -> int | None:
    """Return the smallest threshold strictly greater than ``longest_streak``.

    ``None`` once the user has cleared the final 365-day milestone — there is
    no further chip to dangle in front of them.
    """
    effective = max(0, int(longest_streak))
    for threshold in STREAK_THRESHOLDS:
        if threshold > effective:
            return threshold
    return None
