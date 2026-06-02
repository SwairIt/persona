"""Cluster screenshots into focus sessions by app + temporal proximity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.storage.models import Screenshot


@dataclass(frozen=True, slots=True)
class Session:
    started_at: datetime
    ended_at: datetime
    app_name: str | None
    screenshot_count: int
    sample_titles: list[str]


def build_sessions(
    shots: list[Screenshot],
    *,
    gap_threshold: timedelta = timedelta(minutes=5),
) -> list[Session]:
    """Group consecutive screenshots into sessions.

    A session boundary is drawn when the active app changes OR when the gap
    between consecutive captures exceeds `gap_threshold`.
    """
    if not shots:
        return []

    sorted_shots = sorted(shots, key=lambda s: s.captured_at)
    sessions: list[Session] = []
    current: list[Screenshot] = [sorted_shots[0]]

    for shot in sorted_shots[1:]:
        prev = current[-1]
        same_app = (shot.app_name or "") == (prev.app_name or "")
        within_gap = (shot.captured_at - prev.captured_at) <= gap_threshold
        if same_app and within_gap:
            current.append(shot)
            continue
        sessions.append(_to_session(current))
        current = [shot]

    sessions.append(_to_session(current))
    return sessions


def _to_session(shots: list[Screenshot]) -> Session:
    titles = []
    seen: set[str] = set()
    for s in shots:
        title = (s.window_title or "").strip()
        if title and title not in seen:
            titles.append(title)
            seen.add(title)
        if len(titles) >= 5:
            break
    return Session(
        started_at=shots[0].captured_at,
        ended_at=shots[-1].captured_at,
        app_name=shots[0].app_name,
        screenshot_count=len(shots),
        sample_titles=titles,
    )
