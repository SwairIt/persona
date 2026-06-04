"""Effective-settings resolver — single rule for ``kv_settings`` ↔ env drift (v1.25).

**The rule**: ``kv_settings`` row wins. ``Settings`` (env-loaded) is the
default. Hardcoded ``default`` arg is the last-resort fallback.

For a year Persona accumulated 14 settings that live in BOTH the
pydantic ``Settings`` instance (env-loaded at boot) AND in the runtime
``kv_settings`` table (live-toggleable from the UI). Different callers
read different sources and the result was the v1.24.1 setup-wizard
bug: wizard wrote the env-keyed name, the running loop read the
kv-keyed copy. Resolving "who wins" inside every call site is a
recipe for more bugs — this module centralises the rule.

Usage::

    from app.settings.effective import get_effective_bool, get_effective_int
    interval = await get_effective_float("capture_interval_seconds", default=6.0)
    enabled = await get_effective_bool("ocr_enabled", default=False)

Every public helper opens a short-lived ``get_connection()`` so callers
don't have to thread a connection through. The hot capture loop reads
multiple settings per iteration; rather than 5 separate connections it
should use :func:`get_effective_many` which batches in one connection.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv

log = get_logger("persona.settings.effective")


async def get_effective(
    name: str,
    *,
    default: object | None = None,
) -> object | None:
    """Resolve a setting by name. ``kv_settings`` → ``Settings`` → default.

    Returns the raw value (string for kv, native for Settings).
    Use the typed helpers below for booleans / numbers — they coerce.
    """
    try:
        async with get_connection() as conn:
            kv_value = await get_kv(conn, name)
    except Exception as exc:  # noqa: BLE001
        log.debug("settings.effective.kv_read_failed", name=name, error=str(exc))
        kv_value = None

    if kv_value is not None and str(kv_value).strip() != "":
        return kv_value

    cfg = get_settings()
    if hasattr(cfg, name):
        return getattr(cfg, name)

    return default


async def get_effective_str(name: str, *, default: str = "") -> str:
    """Same as :func:`get_effective` but always returns a stripped ``str``."""
    raw = await get_effective(name, default=default)
    if raw is None:
        return default
    return str(raw).strip()


async def get_effective_bool(name: str, *, default: bool = False) -> bool:
    """Coerce the resolved value into a bool.

    Accepted truthy strings: ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (case-
    insensitive). Anything else is False. For non-string Settings fields
    (already ``bool``) the value is returned directly.
    """
    raw = await get_effective(name, default=default)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


async def get_effective_int(name: str, *, default: int = 0) -> int:
    """Coerce the resolved value into an ``int``. Falls back on ValueError."""
    raw = await get_effective(name, default=default)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        log.debug("settings.effective.int_parse_failed", name=name, value=str(raw)[:80])
        return default


async def get_effective_float(name: str, *, default: float = 0.0) -> float:
    """Coerce the resolved value into a ``float``."""
    raw = await get_effective(name, default=default)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        log.debug("settings.effective.float_parse_failed", name=name, value=str(raw)[:80])
        return default


async def get_effective_many(names: list[str]) -> dict[str, object | None]:
    """Batch helper for hot paths that read multiple settings per iteration.

    Opens one connection, reads all kv rows in a single ``IN (?)`` query,
    fills missing values from ``Settings``. Order of ``names`` is
    preserved in the returned dict.
    """
    if not names:
        return {}

    cfg = get_settings()
    out: dict[str, object | None] = {}
    try:
        async with get_connection() as conn:
            placeholders = ",".join("?" for _ in names)
            cursor = await conn.execute(
                f"SELECT key, value FROM kv_settings WHERE key IN ({placeholders})",  # noqa: S608
                names,
            )
            rows = await cursor.fetchall()
        kv_map = {str(r["key"]): str(r["value"]) for r in rows}
    except Exception as exc:  # noqa: BLE001
        log.debug("settings.effective.batch_kv_read_failed", error=str(exc))
        kv_map = {}

    for name in names:
        if name in kv_map and kv_map[name].strip():
            out[name] = kv_map[name]
        elif hasattr(cfg, name):
            out[name] = getattr(cfg, name)
        else:
            out[name] = None
    return out


def _coerce_bool(value: Any) -> bool:
    """Pure-sync version of :func:`get_effective_bool`'s coercion step.

    Useful when the caller already has the raw value in hand (e.g. from
    :func:`get_effective_many`) and just needs to interpret it.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "_coerce_bool",
    "get_effective",
    "get_effective_bool",
    "get_effective_float",
    "get_effective_int",
    "get_effective_many",
    "get_effective_str",
]
