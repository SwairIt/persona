"""Post-capture analysis — diffs, sessions, streaks."""

from app.analysis.diff import diff_screenshots
from app.analysis.sessions import build_sessions
from app.analysis.streak import StreakSummary, compute_streaks
from app.analysis.time_sheet import (
    AppMinutes,
    compute_per_app_seconds,
    format_duration,
    per_day_total_seconds,
)

__all__ = [
    "AppMinutes",
    "StreakSummary",
    "build_sessions",
    "compute_per_app_seconds",
    "compute_streaks",
    "diff_screenshots",
    "format_duration",
    "per_day_total_seconds",
]
