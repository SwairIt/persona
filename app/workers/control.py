"""Shared control state for background workers — pause/resume/stop signalling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class CaptureController:
    """Process-wide singleton that workers consult for run state."""

    paused: bool = False
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_capture_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_message: str | None = None
    captures_total: int = 0
    captures_skipped_dedup: int = 0
    captures_skipped_idle: int = 0
    captures_failed: int = 0
    next_sleep_seconds: float | None = None

    def request_stop(self) -> None:
        self.stop_event.set()

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def mark_capture(self) -> None:
        self.captures_total += 1
        self.last_capture_at = datetime.now(timezone.utc)

    def mark_dedup_skip(self) -> None:
        self.captures_skipped_dedup += 1

    def mark_idle_skip(self) -> None:
        self.captures_skipped_idle += 1

    def mark_error(self, message: str) -> None:
        self.captures_failed += 1
        self.last_error_at = datetime.now(timezone.utc)
        self.last_error_message = message


_controller: CaptureController | None = None


def get_controller() -> CaptureController:
    global _controller
    if _controller is None:
        _controller = CaptureController()
    return _controller


def reset_controller() -> None:
    """Test helper — clears the singleton."""
    global _controller
    _controller = None
