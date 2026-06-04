"""Background poll that mirrors recent days into an Obsidian vault.

Two kv rows drive the worker:

* ``obsidian_sync_enabled`` — ``"1"`` to enable, anything else means
  the tick is a no-op (default OFF so a fresh install never writes
  outside Persona's own ``data_dir``).
* ``obsidian_vault_path`` — absolute filesystem path to the user's
  vault root. Empty string → tick is a no-op even with the gate on
  (we never default to a synthetic path).

The cadence is 1 hour. Obsidian users tolerate eventual consistency
(this is a notebook, not a chat surface) and an hour is short enough
that "I just took a screenshot, will it show up?" stays in the realm
of "yes, within an hour".

Lifespan wiring lives in the coordinator module — this file only
exposes :func:`run_obsidian_sync_worker`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.logging_setup import get_logger
from app.obsidian_sync import sync_to_vault
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers.heartbeat import beat

log = get_logger("persona.workers.obsidian_sync")

POLL_INTERVAL_SECONDS: float = 3600.0
"""1 hour. Vault sync is a polite background job — bursting this would
just rewrite the same N markdown files."""

LOOKBACK_DAYS: int = 14
"""Each tick re-syncs the last two weeks so a freshly-pinned shot or a
late-arriving daily digest from yesterday lands in the vault without
the user having to hit ``Run now``."""

_HEARTBEAT_NAME: str = "obsidian-sync-worker"

_KV_ENABLED: str = "obsidian_sync_enabled"
_KV_VAULT_PATH: str = "obsidian_vault_path"


async def _read_gate_and_path() -> tuple[bool, str]:
    """Return ``(enabled, vault_path_raw)`` from kv.

    Read failures are swallowed and treated as "disabled" so a
    transient DB hiccup never crashes the loop."""
    try:
        async with get_connection() as conn:
            enabled_raw = await get_kv(conn, _KV_ENABLED)
            path_raw = await get_kv(conn, _KV_VAULT_PATH)
    except Exception as exc:
        log.warning("obsidian_sync.kv.read_failed", error=str(exc))
        return False, ""

    enabled = (enabled_raw or "").strip() == "1"
    path = (path_raw or "").strip()
    return enabled, path


async def _tick() -> dict[str, object]:
    """One worker pass: gate check, sync if enabled and path set."""
    enabled, path_raw = await _read_gate_and_path()
    if not enabled:
        return {"status": "disabled"}
    if not path_raw:
        return {"status": "no_path"}

    try:
        result = await sync_to_vault(Path(path_raw), days=LOOKBACK_DAYS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("obsidian_sync.tick.crashed", error=str(exc))
        return {"status": "error", "error": str(exc)}

    return {
        "status": "ok",
        "files_written": result["files_written"],
        "files_skipped": result["files_skipped"],
        "errors": len(result["errors"]),
    }


async def run_obsidian_sync_worker() -> None:
    """Poll loop. Stops only on :class:`asyncio.CancelledError`."""
    log.info(
        "obsidian_sync.worker.starting",
        poll_seconds=POLL_INTERVAL_SECONDS,
        lookback_days=LOOKBACK_DAYS,
    )
    while True:
        await beat(_HEARTBEAT_NAME)
        try:
            stats = await _tick()
            log.info("obsidian_sync.tick", **stats)
        except asyncio.CancelledError:
            log.info("obsidian_sync.worker.cancelled")
            raise
        except Exception as exc:
            log.exception("obsidian_sync.tick_failed", error=str(exc))

        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("obsidian_sync.worker.cancelled")
            raise


__all__ = [
    "LOOKBACK_DAYS",
    "POLL_INTERVAL_SECONDS",
    "run_obsidian_sync_worker",
]
