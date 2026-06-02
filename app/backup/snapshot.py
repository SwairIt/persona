"""Fernet-encrypted tarball snapshot of Persona state.

Layout of the on-disk artefact produced by :func:`create_backup`:

    [ 16 random salt bytes ][ Fernet token over the gzipped tarball ]

The Fernet key is derived from the user passphrase via PBKDF2-HMAC-SHA256
with 100 000 iterations.  The tarball itself contains:

    data/persona.db          — copy of the SQLite database (after
                               ``PRAGMA wal_checkpoint(FULL)`` so the WAL
                               sidecar is flushed into the main file)
    data/thumbnails/<rel>    — every thumbnail younger than ``days`` days

The format is intentionally tiny and self-contained; restoration only needs
this module plus :mod:`cryptography`.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Final

import anyio

from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger("persona.backup.snapshot")

SALT_BYTES: Final[int] = 16
PBKDF2_ITERATIONS: Final[int] = 100_000
KEY_BYTES: Final[int] = 32
_DAY_SECONDS: Final[int] = 24 * 60 * 60


class BackupNotAvailable(RuntimeError):
    """Raised when the optional :mod:`cryptography` dependency is missing."""


class BackupError(RuntimeError):
    """Raised on any backup / restore failure that is not a missing dep."""


def _require_fernet() -> None:
    """Raise :class:`BackupNotAvailable` when :mod:`cryptography` is missing."""
    try:
        import cryptography.fernet  # noqa: F401, PLC0415 — optional dep probe
    except ImportError as exc:  # pragma: no cover — exercised in env without dep
        msg = (
            "Encrypted backup requires the `cryptography` package. "
            "Install it with `uv add cryptography`."
        )
        raise BackupNotAvailable(msg) from exc


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a urlsafe-base64 32-byte Fernet key from ``password`` + ``salt``."""
    raw = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=KEY_BYTES,
    )
    return base64.urlsafe_b64encode(raw)


def _checkpoint_db(src: Path, dst: Path) -> None:
    """Copy ``src`` SQLite DB to ``dst`` after a full WAL checkpoint."""
    source = sqlite3.connect(str(src))
    try:
        source.execute("PRAGMA wal_checkpoint(FULL)")
        source.commit()
    finally:
        source.close()
    # Use the SQLite Online Backup API so we get a consistent snapshot even
    # if the main process is still writing.
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _build_tarball(
    *,
    db_path: Path,
    thumbnails_dir: Path,
    days: int,
    workdir: Path,
) -> tuple[bytes, int]:
    """Return ``(tarball_bytes, screenshots_count)`` for a snapshot.

    Screenshots count = number of thumbnail files added to the tarball.
    """
    if days < 1:
        msg = f"days must be >= 1, got {days}"
        raise BackupError(msg)

    db_snapshot = workdir / "persona.db"
    _checkpoint_db(db_path, db_snapshot)

    cutoff = time.time() - days * _DAY_SECONDS
    screenshots_count = 0

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(db_snapshot, arcname="data/persona.db")

        if thumbnails_dir.exists():
            for entry in sorted(thumbnails_dir.rglob("*")):
                if not entry.is_file():
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                rel = entry.relative_to(thumbnails_dir).as_posix()
                tar.add(entry, arcname=f"data/thumbnails/{rel}")
                screenshots_count += 1

    return buffer.getvalue(), screenshots_count


def _write_encrypted(
    *,
    out_path: Path,
    payload: bytes,
    password: str,
) -> int:
    """Encrypt ``payload`` and write it to ``out_path``. Return bytes written."""
    _require_fernet()
    from cryptography.fernet import Fernet  # noqa: PLC0415 — guarded above

    salt = os.urandom(SALT_BYTES)
    key = _derive_key(password, salt)
    token = Fernet(key).encrypt(payload)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    blob = salt + token
    out_path.write_bytes(blob)
    return len(blob)


def _create_backup_sync(
    *,
    out_path: Path,
    password: str,
    days: int,
) -> dict[str, object]:
    """Synchronous worker — runs in a thread via :func:`anyio.to_thread.run_sync`."""
    settings = get_settings()
    if not settings.db_path.exists():
        msg = f"database not found at {settings.db_path}"
        raise BackupError(msg)

    with tempfile.TemporaryDirectory(prefix="persona-backup-") as tmpdir:
        workdir = Path(tmpdir)
        tarball, screenshots_count = _build_tarball(
            db_path=settings.db_path,
            thumbnails_dir=settings.thumbnails_dir,
            days=days,
            workdir=workdir,
        )
        size_bytes = _write_encrypted(
            out_path=out_path,
            payload=tarball,
            password=password,
        )

    result: dict[str, object] = {
        "path": str(out_path),
        "size_bytes": size_bytes,
        "screenshots_count": screenshots_count,
    }
    log.info(
        "backup.snapshot.done",
        path=str(out_path),
        size_bytes=size_bytes,
        screenshots=screenshots_count,
        days=days,
    )
    return result


async def create_backup(
    out_path: Path,
    password: str,
    days: int = 30,
) -> dict[str, object]:
    """Create an encrypted snapshot at ``out_path``.

    Args:
        out_path: Destination file. Parent dirs are created if missing.
        password: Passphrase used to derive the Fernet key.
        days: Include thumbnails whose mtime is within the last ``days`` days
            (default 30). The SQLite DB is always included in full.

    Returns:
        A mapping ``{"path", "size_bytes", "screenshots_count"}``.

    Raises:
        BackupNotAvailable: When :mod:`cryptography` cannot be imported.
        BackupError: On any other failure (missing DB, IO error, bad input).
    """
    if not password:
        msg = "password must not be empty"
        raise BackupError(msg)
    # Force the import check on the event loop so callers get a clean error
    # before we touch the filesystem.
    _require_fernet()

    return await anyio.to_thread.run_sync(
        lambda: _create_backup_sync(
            out_path=out_path,
            password=password,
            days=days,
        )
    )


def _decrypt_blob(blob: bytes, password: str) -> bytes:
    """Reverse the format written by :func:`_write_encrypted`."""
    _require_fernet()
    from cryptography.fernet import Fernet, InvalidToken  # noqa: PLC0415 — guarded

    if len(blob) <= SALT_BYTES:
        msg = "backup file is truncated"
        raise BackupError(msg)

    salt = blob[:SALT_BYTES]
    token = blob[SALT_BYTES:]
    key = _derive_key(password, salt)

    try:
        return bytes(Fernet(key).decrypt(token))
    except InvalidToken as exc:
        msg = "wrong password or corrupted backup file"
        raise BackupError(msg) from exc


def _restore_backup_sync(
    *,
    in_path: Path,
    password: str,
    force: bool,
) -> dict[str, object]:
    """Synchronous worker — runs in a thread."""
    if not in_path.exists():
        msg = f"backup file not found at {in_path}"
        raise BackupError(msg)

    settings = get_settings()
    db_path = settings.db_path
    thumbnails_dir = settings.thumbnails_dir

    if db_path.exists() and not force:
        msg = (
            f"refusing to overwrite existing database at {db_path}; "
            "pass force=True to proceed"
        )
        raise BackupError(msg)

    payload = _decrypt_blob(in_path.read_bytes(), password)

    restored_files = 0
    screenshots_count = 0
    with tempfile.TemporaryDirectory(prefix="persona-restore-") as tmpdir:
        extract_root = Path(tmpdir)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            # Defuse path-traversal entries before extracting.
            for member in tar.getmembers():
                name = member.name.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    msg = f"refusing unsafe tar entry: {member.name!r}"
                    raise BackupError(msg)
            tar.extractall(extract_root)  # noqa: S202 — validated above

        src_db = extract_root / "data" / "persona.db"
        if not src_db.exists():
            msg = "backup is missing data/persona.db"
            raise BackupError(msg)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove WAL sidecars left over from a prior crash so we start clean.
        for sidecar in (db_path, db_path.with_suffix(db_path.suffix + "-wal"),
                        db_path.with_suffix(db_path.suffix + "-shm")):
            if sidecar.exists():
                sidecar.unlink()
        shutil.copy2(src_db, db_path)
        restored_files += 1

        src_thumbs = extract_root / "data" / "thumbnails"
        if src_thumbs.exists():
            thumbnails_dir.mkdir(parents=True, exist_ok=True)
            for entry in src_thumbs.rglob("*"):
                if not entry.is_file():
                    continue
                rel = entry.relative_to(src_thumbs)
                target = thumbnails_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target)
                restored_files += 1
                screenshots_count += 1

    result: dict[str, object] = {
        "path": str(in_path),
        "size_bytes": in_path.stat().st_size,
        "screenshots_count": screenshots_count,
        "restored_files": restored_files,
    }
    log.info(
        "backup.restore.done",
        path=str(in_path),
        restored_files=restored_files,
        screenshots=screenshots_count,
    )
    return result


async def restore_backup(
    in_path: Path,
    password: str,
    force: bool = False,
) -> dict[str, object]:
    """Restore a snapshot written by :func:`create_backup`.

    Args:
        in_path: Source archive file.
        password: Passphrase used at backup time.
        force: When ``False`` (default), refuse to overwrite an existing
            database. When ``True``, replace it.

    Returns:
        A mapping ``{"path", "size_bytes", "screenshots_count",
        "restored_files"}``.

    Raises:
        BackupNotAvailable: When :mod:`cryptography` cannot be imported.
        BackupError: Wrong password, corrupted file, missing source DB,
            unsafe tar entry, or refusal to overwrite without ``force``.
    """
    if not password:
        msg = "password must not be empty"
        raise BackupError(msg)
    _require_fernet()

    return await anyio.to_thread.run_sync(
        lambda: _restore_backup_sync(
            in_path=in_path,
            password=password,
            force=force,
        )
    )


__all__ = [
    "BackupError",
    "BackupNotAvailable",
    "create_backup",
    "restore_backup",
]
