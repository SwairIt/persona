"""Application boundary for owner-controlled browser automation."""

from app.application.automation.contracts import (
    ALLOWED_BROWSER_ACTIONS,
    BrowserAction,
    BrowserActionError,
    BrowserCommand,
    BrowserJob,
)
from app.application.automation.service import (
    BrowserExecutionError,
    BrowserExecutionTimeout,
    RemoteBrowserService,
)

__all__ = [
    "ALLOWED_BROWSER_ACTIONS",
    "BrowserAction",
    "BrowserActionError",
    "BrowserCommand",
    "BrowserExecutionError",
    "BrowserExecutionTimeout",
    "BrowserJob",
    "RemoteBrowserService",
]
