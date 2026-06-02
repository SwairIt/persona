"""Screen capture via mss — single monitor or multi-monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import mss
from PIL import Image


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """One captured frame with metadata."""

    image: Image.Image
    width: int
    height: int
    captured_at: datetime
    monitor_index: int


def capture_primary_monitor() -> CaptureResult:
    """Capture the primary monitor and return a Pillow image with metadata."""
    with mss.mss() as sct:
        monitor: dict[str, int] = sct.monitors[1]
        shot: Any = sct.grab(monitor)
        image = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
        return CaptureResult(
            image=image,
            width=shot.width,
            height=shot.height,
            captured_at=datetime.now(timezone.utc),
            monitor_index=0,
        )


def capture_all_monitors() -> list[CaptureResult]:
    """Capture every connected monitor as a separate CaptureResult.

    Returns an empty list if there are no monitors detected.
    """
    results: list[CaptureResult] = []
    now = datetime.now(timezone.utc)
    with mss.mss() as sct:
        for idx, monitor in enumerate(sct.monitors[1:]):
            try:
                shot: Any = sct.grab(monitor)
                image = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
                results.append(
                    CaptureResult(
                        image=image,
                        width=shot.width,
                        height=shot.height,
                        captured_at=now,
                        monitor_index=idx,
                    )
                )
            except (OSError, mss.exception.ScreenShotError):
                continue
    return results


def list_monitors() -> list[dict[str, int]]:
    """Return list of monitor geometries (excluding the virtual all-monitors entry)."""
    with mss.mss() as sct:
        return [dict(m) for m in sct.monitors[1:]]
