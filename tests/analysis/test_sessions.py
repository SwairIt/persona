"""Tests for session-clustering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.sessions import build_sessions
from app.storage.models import Screenshot


def _make_shot(
    sid: int,
    captured_at: datetime,
    app_name: str = "VS Code",
    window_title: str = "main.py",
) -> Screenshot:
    return Screenshot(
        id=sid,
        captured_at=captured_at,
        monitor_index=0,
        width=1920,
        height=1080,
        thumbnail_path=None,
        phash=f"{sid:016d}",
        app_name=app_name,
        window_title=window_title,
        process_name="code.exe",
        ocr_status="done",
        ocr_text=None,
        dedup_group_id=None,
        created_at=captured_at,
    )


def test_build_sessions_empty() -> None:
    assert build_sessions([]) == []


def test_single_continuous_session() -> None:
    base = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    shots = [_make_shot(i, base + timedelta(seconds=i * 30)) for i in range(5)]
    sessions = build_sessions(shots)
    assert len(sessions) == 1
    assert sessions[0].screenshot_count == 5


def test_split_on_app_change() -> None:
    base = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    a = _make_shot(1, base, app_name="VS Code")
    b = _make_shot(2, base + timedelta(seconds=30), app_name="Chrome")
    c = _make_shot(3, base + timedelta(seconds=60), app_name="Chrome")
    sessions = build_sessions([a, b, c])
    assert len(sessions) == 2
    assert sessions[0].app_name == "VS Code"
    assert sessions[1].app_name == "Chrome"
    assert sessions[1].screenshot_count == 2


def test_split_on_long_gap() -> None:
    base = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    a = _make_shot(1, base, app_name="VS Code")
    b = _make_shot(2, base + timedelta(minutes=15), app_name="VS Code")
    sessions = build_sessions([a, b], gap_threshold=timedelta(minutes=5))
    assert len(sessions) == 2


def test_session_collects_sample_titles() -> None:
    base = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    shots = [
        _make_shot(1, base, window_title="main.py"),
        _make_shot(2, base + timedelta(seconds=30), window_title="utils.py"),
        _make_shot(3, base + timedelta(seconds=60), window_title="main.py"),
    ]
    sessions = build_sessions(shots)
    assert sessions[0].sample_titles == ["main.py", "utils.py"]
