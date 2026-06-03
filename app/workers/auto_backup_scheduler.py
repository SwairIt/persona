"""Nightly encrypted DB backup scheduler — opt-in, vault-driven.

Reuses the v0.23 backup CLI logic (:func:`app.backup.snapshot.create_backup`)
to write an encrypted tarball snapshot of the SQLite DB plus recent
thumbnails to ``settings.auto_backup_path`` once per local day, at the
hour configured by ``settings.auto_backup_hour_local`` (default ``03``).

Behaviour
=========

* Polls every 30 minutes (matches the other half-hour schedulers).
* Fires at most once per local calendar date — the run-marker lives in
  ``kv_settings`` under :data:`_LAST_RUN_KEY` so the 30-minute poll
  cannot double-snapshot inside the same hour, and a process restart
  still respects an already-completed day.
* The passphrase is read from the v0.33 vault under the key
  :data:`_VAULT_KEY` (``"auto_backup_password"``). The vault is itself
  encrypted under a master password — the worker reads that master
  password from ``$PERSONA_VAULT_MASTER_PASSWORD``. When either the env
  var, the vault row, or the optional :mod:`cryptography` dependency is
  missing, the loop logs a structured warning and parks until the next
  tick. Failures are NEVER raised through to the caller — the lifespan
  must stay alive even if backups are mis-configured.
* After a successful snapshot, files in ``auto_backup_path`` matching
  ``persona-backup-*.bin`` whose mtime is older than
  ``settings.auto_backup_keep_days`` days are removed. The current
  run's freshly-written file is always kept.

Naming convention
=================

Snapshots are written as ``persona-backup-YYYY-MM-DD.bin`` (one per
local day). Re-running on the same day overwrites the file — the
``kv`` idempotency guard normally prevents that, but if the marker
gets deleted manually the on-disk artefact is still consistent.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, Final

from app.backup.snapshot import BackupError, BackupNotAvailable, create_backup
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.vault import get_secret
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger("persona.auto_backup")

POLL_INTERVAL_SECONDS: Final[float] = 1800.0  # 30 minutes
_LAST_RUN_KEY: Final[str] = "auto_backup_last_run"
_VAULT_KEY: Final[str] = "auto_backup_password"
_MASTER_PASSWORD_ENV: Final[str] = "PERSONA_VAULT_MASTER_PASSWORD"  # noqa: S105 — env var NAME
_BACKUP_FILENAME_PREFIX: Final[str] = "persona-backup-"
_BACKUP_FILENAME_SUFFIX: Final[str] = ".bin"
_DAY_SECONDS: Final[int] = 24 * 60 * 60
# Mirrors ``create_backup``'s default — include thumbnails written in the
# last 30 days. Older thumbnails live in archive tiers and are excluded
# from the nightly snapshot to keep file sizes sane.
_THUMBNAIL_WINDOW_DAYS: Final[int] = 30


async def run_auto_backup_scheduler(
    controller: CaptureController | None = None,
) -> None:
    """Long-running loop. Yields on ``controller.stop_event``."""
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.auto_backup_enabled:
        log.info("auto_backup.disabled")
        await ctrl.stop_event.wait()
        return

    log.info(
        "auto_backup.started",
        hour=settings.auto_backup_hour_local,
        path=str(settings.auto_backup_path),
        keep_days=settings.auto_backup_keep_days,
    )

    while not ctrl.stop_event.is_set():
        await beat("auto-backup-scheduler")
        try:
            await _maybe_backup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The worker must outlive every kind of failure — log and
            # continue so the next tick gets a chance after the
            # transient issue clears.
            log.exception("auto_backup.failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue


async def _maybe_backup() -> None:
    """One poll iteration — decide whether to snapshot + prune."""
    settings = get_settings()
    now_local = datetime.now().astimezone()

    if now_local.hour != settings.auto_backup_hour_local:
        return

    today_iso = now_local.date().isoformat()

    async with get_connection() as conn:
        last_run = await get_kv(conn, _LAST_RUN_KEY)
    if last_run == today_iso:
        return

    passphrase = await _fetch_passphrase()
    if passphrase is None:
        return

    out_path = settings.auto_backup_path / (
        f"{_BACKUP_FILENAME_PREFIX}{today_iso}{_BACKUP_FILENAME_SUFFIX}"
    )

    log.info("auto_backup.run.start", path=str(out_path))
    try:
        result = await create_backup(
            out_path=out_path,
            password=passphrase,
            days=_THUMBNAIL_WINDOW_DAYS,
        )
    except BackupNotAvailable:
        # Race: the ``cryptography`` probe in ``get_secret`` succeeded
        # but the import inside ``create_backup`` failed. Treat the
        # same as the vault missing-dep path.
        log.warning("auto_backup.skipped", reason="missing_dep")
        return
    except BackupError as exc:
        # Bad input, missing DB, IO error — log and bail. We deliberately
        # do NOT advance the kv marker so the next tick (after the user
        # fixes the underlying issue) gets a chance to retry.
        log.warning(
            "auto_backup.skipped",
            reason="backup_error",
            error=str(exc),
        )
        return

    async with get_connection() as conn:
        await set_kv(conn, _LAST_RUN_KEY, today_iso)

    raw_size = result.get("size_bytes", 0)
    raw_count = result.get("screenshots_count", 0)
    size_bytes = int(raw_size) if isinstance(raw_size, int) else 0
    screenshots = int(raw_count) if isinstance(raw_count, int) else 0
    log.info(
        "auto_backup.run.done",
        path=str(result.get("path", out_path)),
        size_bytes=size_bytes,
        screenshots=screenshots,
    )

    pruned = _prune_old_backups(
        directory=settings.auto_backup_path,
        keep_days=settings.auto_backup_keep_days,
        keep_file=out_path,
    )
    if pruned:
        log.info("auto_backup.prune.done", removed=pruned)


async def _fetch_passphrase() -> str | None:
    """Return the snapshot passphrase or ``None`` when unavailable.

    The worker is silent on every "config not ready" failure mode —
    missing master password env var, missing vault row, wrong master
    password, missing :mod:`cryptography` dep, or an empty stored
    value. Each branch logs a structured warning with a stable
    ``reason`` field so ``/admin/health`` can surface the cause.
    """
    master_password = os.environ.get(_MASTER_PASSWORD_ENV, "")
    if not master_password:
        log.warning("auto_backup.skipped", reason="missing_master_password")
        return None

    try:
        secret = await get_secret(_VAULT_KEY, master_password)
    except Exception as exc:
        log.warning(
            "auto_backup.skipped",
            reason="vault_error",
            error=str(exc),
        )
        return None

    status = str(secret.get("status", "unknown"))
    if status == "ok":
        passphrase = str(secret.get("value", ""))
        if not passphrase:
            log.warning("auto_backup.skipped", reason="empty_passphrase")
            return None
        return passphrase

    reason_map: dict[str, str] = {
        "missing_dep": "missing_dep",
        "not_found": "vault_key_missing",
        "wrong_password": "wrong_master_password",
    }
    reason = reason_map.get(status, "vault_status")
    if reason == "vault_status":
        log.warning("auto_backup.skipped", reason=reason, status=status)
    else:
        log.warning("auto_backup.skipped", reason=reason)
    return None


def _prune_old_backups(
    *,
    directory: Path,
    keep_days: int,
    keep_file: Path,
) -> int:
    """Remove ``persona-backup-*.bin`` files older than ``keep_days``.

    Args:
        directory: Folder containing the snapshot files.
        keep_days: Maximum age in days; files with ``mtime`` older than
            this cutoff are unlinked.
        keep_file: Path that must NEVER be removed (the run just
            completed) — guards against an edge case where the system
            clock made the new file look ancient.

    Returns:
        Count of files actually removed. ``0`` when nothing matched or
        the directory does not yet exist.
    """
    if keep_days <= 0:
        return 0
    if not directory.exists():
        return 0

    cutoff = time.time() - keep_days * _DAY_SECONDS
    keep_resolved = keep_file.resolve() if keep_file.exists() else keep_file
    removed = 0

    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not (
            name.startswith(_BACKUP_FILENAME_PREFIX)
            and name.endswith(_BACKUP_FILENAME_SUFFIX)
        ):
            continue
        try:
            if entry.resolve() == keep_resolved:
                continue
        except OSError:
            # Path resolution can fail for symlink loops; fall through
            # to the name check instead.
            if entry == keep_file:
                continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            entry.unlink()
        except OSError as exc:
            log.warning(
                "auto_backup.prune.failed",
                path=str(entry),
                error=str(exc),
            )
            continue
        removed += 1

    return removed


__all__ = ["run_auto_backup_scheduler"]
