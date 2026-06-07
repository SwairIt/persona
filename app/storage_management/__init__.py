"""Server-side storage maintenance — cleanup of old screenshots + audit log.

This package lives next to ``app/storage/`` (low-level DB plumbing) but
operates at a higher level: it knows about user-facing retention policy
(``kv.shots_retention_days``), screenshots that have aged out, and the
audit trail of cleanup runs.

Public surface:

  * :func:`get_settings`       — current retention + quota policy
  * :func:`set_settings`       — update policy from /storage page
  * :func:`usage_breakdown`    — what's eating disk space, by category
  * :func:`run_cleanup`        — execute one cleanup pass (worker or manual)
  * :func:`list_cleanup_runs`  — audit log for the dashboard
"""

from app.storage_management.cleanup import (
    StorageSettings,
    UsageBreakdown,
    get_settings,
    list_cleanup_runs,
    run_cleanup,
    set_settings,
    usage_breakdown,
)

__all__ = [
    "StorageSettings",
    "UsageBreakdown",
    "get_settings",
    "list_cleanup_runs",
    "run_cleanup",
    "set_settings",
    "usage_breakdown",
]
