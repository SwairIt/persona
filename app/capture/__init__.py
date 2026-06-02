"""Screen, window and idle capture primitives."""

from app.capture.icons import ensure_icon_cached, icon_path_for
from app.capture.idle import seconds_since_last_input
from app.capture.screen import (
    CaptureResult,
    capture_all_monitors,
    capture_primary_monitor,
    list_monitors,
)
from app.capture.whitelist import (
    default_deny_list,
    load_user_lists,
    save_user_lists,
    should_capture,
)
from app.capture.window import ActiveWindow, get_active_window

__all__ = [
    "ActiveWindow",
    "CaptureResult",
    "capture_all_monitors",
    "capture_primary_monitor",
    "default_deny_list",
    "ensure_icon_cached",
    "get_active_window",
    "icon_path_for",
    "list_monitors",
    "load_user_lists",
    "save_user_lists",
    "seconds_since_last_input",
    "should_capture",
]
