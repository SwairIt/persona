"""Per-device storage budget + sync filter helpers (T13/T14).

This module is the policy layer between ``/devices/{id}/storage`` (the
HTML UI), the JSON API used by the Mac/iOS agents, and the existing
``/api/sync/pull`` route. It does NOT enforce anything itself — the
sync_api filter logic and the storage_enforcement_worker call into here
to read the rules.

Three operations matter:

  * :func:`get_policy` — fetch the current quota / retention / role
    for a single device. Returns sensible defaults when no row exists
    so the rest of the codebase never has to special-case "policy not
    set yet".
  * :func:`set_policy` — upsert the policy row. The user calls this
    from the /devices/{id}/storage form.
  * :func:`allowed_kinds_for_device` — return the set of sync_event
    kinds this device is OPTED IN to receive. The /api/sync/pull route
    filters by this so a "viewer" iPhone doesn't get 30 GB of OCR-text
    streaming events.
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.devices.storage_policy")

# All sync kinds the system knows about. Used as the source of truth
# when building the per-(device, kind) filter UI — if we add a new kind
# in a future migration, we add the literal here and the UI surfaces it.
ALL_SYNC_KINDS: tuple[str, ...] = (
    "note",
    "kv",
    "tag",
    "annotation",
    "shot_tag",
    "shot_blob",
)

# Per-role defaults. The UI auto-fills these when the user picks a role
# from the dropdown — they can still override before saving.
ROLE_DEFAULTS: dict[str, dict[str, int | None]] = {
    # Main capturing device: keep everything locally forever.
    "primary":  {"quota_mb": None, "retention_days": None},
    # Long-term backup: also no cap.
    "archive":  {"quota_mb": None, "retention_days": None},
    # Phone / iPad — read-only viewer with tight budgets.
    "viewer":   {"quota_mb": 500, "retention_days": 7},
    # Sync-but-don't-show — e.g. an old machine kept warm just for
    # redundancy. Tighter than viewer.
    "passive":  {"quota_mb": 200, "retention_days": 14},
}


class StoragePolicy(TypedDict):
    device_id: int
    quota_mb: int | None
    retention_days: int | None
    role: str
    updated_at: str | None


def _row_to_policy(row: Any) -> StoragePolicy:
    return {
        "device_id": int(row["device_id"]),
        "quota_mb": int(row["quota_mb"]) if row["quota_mb"] is not None else None,
        "retention_days": (
            int(row["retention_days"]) if row["retention_days"] is not None else None
        ),
        "role": str(row["role"]),
        "updated_at": str(row["updated_at"]) if row["updated_at"] is not None else None,
    }


def _default_policy(device_id: int) -> StoragePolicy:
    return {
        "device_id": device_id,
        "quota_mb": None,
        "retention_days": None,
        "role": "primary",
        "updated_at": None,
    }


async def get_policy(device_id: int) -> StoragePolicy:
    """Read the policy row for one device. Returns a primary-role default
    when no row has been written — the rest of the system treats absent
    and explicit-primary-with-no-cap the same way."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM device_storage_policy WHERE device_id = ?",
            (device_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return _default_policy(device_id)
    return _row_to_policy(row)


async def set_policy(
    device_id: int,
    *,
    role: str,
    quota_mb: int | None,
    retention_days: int | None,
) -> StoragePolicy:
    """Upsert the policy row. Validates role against the CHECK list — a
    bogus value would raise inside SQLite but we want a clean ValueError
    at the route layer."""
    if role not in ROLE_DEFAULTS:
        raise ValueError(
            f"unknown role {role!r}; expected one of {sorted(ROLE_DEFAULTS)}"
        )
    # Clamp the numerics — negative quotas / retention are nonsense, and
    # absurdly large values almost always mean "no cap".
    if quota_mb is not None:
        quota_mb = max(0, min(int(quota_mb), 10_000_000))
        if quota_mb == 0:
            quota_mb = None  # treat 0 as "no limit"
    if retention_days is not None:
        retention_days = max(0, min(int(retention_days), 36_500))  # 100y
        if retention_days == 0:
            retention_days = None

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO device_storage_policy "
            "  (device_id, role, quota_mb, retention_days, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "  role = excluded.role, "
            "  quota_mb = excluded.quota_mb, "
            "  retention_days = excluded.retention_days, "
            "  updated_at = excluded.updated_at",
            (device_id, role, quota_mb, retention_days),
        )
        await conn.commit()
    return await get_policy(device_id)


async def list_sync_filters(device_id: int) -> dict[str, bool]:
    """Return ``{kind: enabled}`` for every known kind. Missing rows
    default to ``True`` — the absence of a filter row means "default,
    sync this kind"."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT kind, enabled FROM device_sync_filter WHERE device_id = ?",
            (device_id,),
        )
        rows = await cursor.fetchall()
    overrides = {str(r["kind"]): bool(r["enabled"]) for r in rows}
    return {k: overrides.get(k, True) for k in ALL_SYNC_KINDS}


async def set_sync_filter(device_id: int, kind: str, enabled: bool) -> None:
    """Upsert one (device, kind) row. Used by the /devices/{id}/storage
    form so the user can untick "stream OCR text to my iPhone"."""
    if kind not in ALL_SYNC_KINDS:
        raise ValueError(
            f"unknown sync kind {kind!r}; expected one of {ALL_SYNC_KINDS}"
        )
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO device_sync_filter (device_id, kind, enabled) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(device_id, kind) DO UPDATE SET enabled = excluded.enabled",
            (device_id, kind, 1 if enabled else 0),
        )
        await conn.commit()


async def allowed_kinds_for_device(device_id: int) -> frozenset[str]:
    """Used by /api/sync/pull to filter events. Default: every kind."""
    flags = await list_sync_filters(device_id)
    return frozenset(k for k, v in flags.items() if v)


async def apply_role_defaults(device_id: int, role: str) -> StoragePolicy:
    """Convenience: pick the canonical quota / retention for a role.

    Used by the UI button "Apply preset for this role" so the user
    doesn't have to memorise the magic numbers."""
    if role not in ROLE_DEFAULTS:
        raise ValueError(f"unknown role {role!r}")
    defaults = ROLE_DEFAULTS[role]
    return await set_policy(
        device_id,
        role=role,
        quota_mb=defaults["quota_mb"],
        retention_days=defaults["retention_days"],
    )
