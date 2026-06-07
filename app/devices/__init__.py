"""Device-row CRUD + heartbeat + per-device storage policy.

This package was originally a single module (``app/devices.py``) that
held just the device CRUD. T13 (2026-06-07) introduced per-device
storage quotas and selective sync filters, which warranted a sibling
module — so the file was promoted to a package. The original CRUD
helpers (``register_device``, ``heartbeat``, etc.) re-exported here
verbatim so every existing call site keeps working.

  * Device CRUD + heartbeat → :mod:`app.devices.core`
  * Storage quotas + sync filters → :mod:`app.devices.storage_policy`
"""

from app.devices.core import (
    DeviceRow,
    delete_device,
    get_device,
    heartbeat,
    list_devices,
    lookup_by_token,
    register_device,
    rename_device,
    rotate_token,
    set_capture_interval,
    set_capture_paused,
)
from app.devices.storage_policy import (
    ALL_SYNC_KINDS,
    ROLE_DEFAULTS,
    StoragePolicy,
    allowed_kinds_for_device,
    apply_role_defaults,
    get_policy,
    list_sync_filters,
    set_policy,
    set_sync_filter,
)

__all__ = [
    # core CRUD (preserved from app/devices.py)
    "DeviceRow",
    "delete_device",
    "get_device",
    "heartbeat",
    "list_devices",
    "lookup_by_token",
    "register_device",
    "rename_device",
    "rotate_token",
    "set_capture_interval",
    "set_capture_paused",
    # T13 storage policy
    "ALL_SYNC_KINDS",
    "ROLE_DEFAULTS",
    "StoragePolicy",
    "allowed_kinds_for_device",
    "apply_role_defaults",
    "get_policy",
    "list_sync_filters",
    "set_policy",
    "set_sync_filter",
]
