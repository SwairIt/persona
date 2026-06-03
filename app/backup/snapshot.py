"""Fernet-encrypted tarball snapshot of Persona state.

Layout of the on-disk artefact produced by :func:`create_backup`
since v0.92::

    [ version_byte ][ 16 random salt bytes ][ Fernet token over .tar.gz ]

``version_byte`` is the KDF version tag:

* ``0x02`` — current 600 000-iteration PBKDF2-HMAC-SHA256 (default).
* ``0x01`` — legacy 100 000 iterations; only emitted by pre-v0.92
  builds. The decrypt path still understands it so old backups keep
  restoring.

Backups produced before the version byte was introduced have no leading
tag at all — they start straight with the 16-byte salt. :func:`_decrypt_blob`
auto-detects that shape and falls back to 100k iterations, so the upgrade
is fully backwards compatible.

The Fernet key is derived from the user passphrase via PBKDF2-HMAC-SHA256
with the iteration count selected by the version byte.  The tarball
itself contains:

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

from app.audit import log_action
from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger("persona.backup.snapshot")
log_verify = get_logger("persona.backup.verify")
log_kdf = get_logger("persona.kdf.upgrade")

SALT_BYTES: Final[int] = 16
# KDF version byte → PBKDF2 iteration count.
#   * ``0x01`` — legacy 100k, only honoured by the decrypt path.
#   * ``0x02`` — current 600k, written by every new backup.
# Backups produced before any version byte existed have no leading tag;
# :func:`_decrypt_blob` auto-detects that shape and treats it as legacy.
KDF_VERSION_LEGACY: Final[int] = 0x01
KDF_VERSION_CURRENT: Final[int] = 0x02
KDF_ITERATIONS_BY_VERSION: Final[dict[int, int]] = {
    KDF_VERSION_LEGACY: 100_000,
    KDF_VERSION_CURRENT: 600_000,
}
# Public constant: the *current* work factor. Older builds imported this
# expecting 100k; bumping it to 600k is the visible part of the v0.92
# strengthening. Callers that read it for sanity checks / metrics will
# automatically pick up the upgraded value.
PBKDF2_ITERATIONS: Final[int] = KDF_ITERATIONS_BY_VERSION[KDF_VERSION_CURRENT]
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


def _derive_key(
    password: str,
    salt: bytes,
    iterations: int = PBKDF2_ITERATIONS,
) -> bytes:
    """Derive a urlsafe-base64 32-byte Fernet key from ``password`` + ``salt``.

    ``iterations`` defaults to the current work factor (600k). The
    decrypt path passes the legacy 100k count when it spots a
    pre-v0.92 envelope (either tagged ``0x01`` or untagged).
    """
    raw = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=KEY_BYTES,
    )
    return base64.urlsafe_b64encode(raw)


def _split_blob(blob: bytes) -> tuple[int, bytes, bytes] | None:
    """Return ``(version, salt, token)`` for an on-disk backup blob.

    Accepts both shapes:

    * ``[version][salt(16)][token]`` — v0.92+.
    * ``[salt(16)][token]``          — untagged pre-v0.92 (legacy 100k).

    Returns ``None`` when the blob is too short to even hold a salt.
    """
    if len(blob) <= SALT_BYTES:
        return None
    leading = blob[0]
    if leading in KDF_ITERATIONS_BY_VERSION and len(blob) > 1 + SALT_BYTES:
        salt = blob[1 : 1 + SALT_BYTES]
        token = blob[1 + SALT_BYTES :]
        return leading, salt, token
    return KDF_VERSION_LEGACY, blob[:SALT_BYTES], blob[SALT_BYTES:]


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
    key = _derive_key(password, salt, PBKDF2_ITERATIONS)
    token = Fernet(key).encrypt(payload)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    blob = bytes([KDF_VERSION_CURRENT]) + salt + token
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


def _decrypt_blob(blob: bytes, password: str) -> tuple[bytes, int]:
    """Reverse the format written by :func:`_write_encrypted`.

    Returns ``(payload, kdf_version)`` so the caller can surface which
    iteration count was used — needed for the v0.92 audit trail when
    restoring an older backup.
    """
    _require_fernet()
    from cryptography.fernet import Fernet, InvalidToken  # noqa: PLC0415 — guarded

    split = _split_blob(blob)
    if split is None:
        msg = "backup file is truncated"
        raise BackupError(msg)

    version, salt, token = split
    iterations = KDF_ITERATIONS_BY_VERSION[version]
    key = _derive_key(password, salt, iterations)

    try:
        payload = bytes(Fernet(key).decrypt(token))
    except InvalidToken as exc:
        msg = "wrong password or corrupted backup file"
        raise BackupError(msg) from exc

    if version != KDF_VERSION_CURRENT:
        log_kdf.info(
            "backup.kdf.legacy_read",
            kdf_version=version,
            iterations=iterations,
            current_iterations=PBKDF2_ITERATIONS,
        )
    return payload, version


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

    payload, kdf_version = _decrypt_blob(in_path.read_bytes(), password)

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
        "kdf_version": kdf_version,
    }
    log.info(
        "backup.restore.done",
        path=str(in_path),
        restored_files=restored_files,
        screenshots=screenshots_count,
        kdf_version=kdf_version,
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

    result = await anyio.to_thread.run_sync(
        lambda: _restore_backup_sync(
            in_path=in_path,
            password=password,
            force=force,
        )
    )
    await _audit_kdf_version(action="backup.restore", in_path=in_path, result=result)
    return result


async def _audit_kdf_version(
    *,
    action: str,
    in_path: Path,
    result: dict[str, object],
) -> None:
    """Emit a ``persona.kdf.upgrade`` audit row when a legacy backup is opened.

    Reads ``result["kdf_version"]`` (set by the sync workers) and, when
    it is anything other than :data:`KDF_VERSION_CURRENT`, files an audit
    entry so an operator can see that a pre-v0.92 archive was decrypted
    with the legacy 100k work factor.
    """
    raw_version = result.get("kdf_version", KDF_VERSION_CURRENT)
    version = int(raw_version) if isinstance(raw_version, int) else KDF_VERSION_CURRENT
    if version == KDF_VERSION_CURRENT:
        return
    iterations = KDF_ITERATIONS_BY_VERSION.get(version, 0)
    await log_action(
        action=f"{action}.kdf.legacy_read",
        target=str(in_path),
        detail=f"version={version} iterations={iterations}",
    )


def _verify_backup_sync(
    *,
    in_path: Path,
    password: str,
) -> dict[str, object]:
    """Synchronous worker — runs in a thread.

    Decrypt ``in_path`` to memory, extract the inner tarball to a temp
    directory (so the live DB is *never* touched), then sanity-check the
    contents:

    * the manifest entry ``data/persona.db`` exists and is openable via
      :mod:`sqlite3` (``PRAGMA integrity_check`` runs cleanly);
    * every other tar entry is a safe relative path (defuses traversal);
    * thumbnails under ``data/thumbnails/`` are counted as ``screenshots``.

    Returns a mapping ``{"status", "files", "db_ok", "screenshots_count"}``
    where ``status`` is ``"ok"`` on success.
    """
    if not in_path.exists():
        msg = f"backup file not found at {in_path}"
        raise BackupError(msg)

    payload, kdf_version = _decrypt_blob(in_path.read_bytes(), password)

    files = 0
    screenshots_count = 0
    db_ok = False

    with tempfile.TemporaryDirectory(prefix="persona-verify-") as tmpdir:
        extract_root = Path(tmpdir)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            members = tar.getmembers()
            # Defuse path-traversal entries before extracting — same
            # policy as :func:`_restore_backup_sync`.
            for member in members:
                name = member.name.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    msg = f"refusing unsafe tar entry: {member.name!r}"
                    raise BackupError(msg)
            tar.extractall(extract_root)  # noqa: S202 — validated above

            for member in members:
                if not member.isfile():
                    continue
                files += 1
                arc = member.name.replace("\\", "/")
                if arc.startswith("data/thumbnails/"):
                    screenshots_count += 1

        src_db = extract_root / "data" / "persona.db"
        if not src_db.exists():
            msg = "backup is missing data/persona.db"
            raise BackupError(msg)

        # Open the extracted DB read-only and run a fast integrity probe.
        # ``PRAGMA integrity_check`` returns the single row ``ok`` on a
        # clean file; anything else means the snapshot is suspect.
        conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
        try:
            cursor = conn.execute("PRAGMA integrity_check")
            row = cursor.fetchone()
            db_ok = bool(row) and str(row[0]).lower() == "ok"
        except sqlite3.DatabaseError:
            db_ok = False
        finally:
            conn.close()

    result: dict[str, object] = {
        "status": "ok",
        "files": files,
        "db_ok": db_ok,
        "screenshots_count": screenshots_count,
        "kdf_version": kdf_version,
    }
    log_verify.info(
        "backup.verify.done",
        path=str(in_path),
        files=files,
        db_ok=db_ok,
        screenshots=screenshots_count,
        kdf_version=kdf_version,
    )
    return result


async def verify_backup(
    in_path: Path,
    password: str,
) -> dict[str, object]:
    """Decrypt and inspect a snapshot without restoring it.

    Opens ``in_path`` with ``password``, extracts the inner tarball into
    a temporary directory, checks that the SQLite database is openable
    via :mod:`sqlite3` (``PRAGMA integrity_check`` returns ``ok``), and
    counts tar entries.  The live database and thumbnails directory are
    never touched.

    Args:
        in_path: Source archive file (the one ``create_backup`` produced).
        password: Passphrase used at backup time.

    Returns:
        A mapping ``{"status", "files", "db_ok", "screenshots_count"}``.
        ``status`` is always ``"ok"`` on success; failures raise instead.

    Raises:
        BackupNotAvailable: When :mod:`cryptography` cannot be imported.
        BackupError: Wrong password, corrupted file, unsafe tar entry,
            or missing ``data/persona.db`` member.
    """
    if not password:
        msg = "password must not be empty"
        raise BackupError(msg)
    _require_fernet()

    result = await anyio.to_thread.run_sync(
        lambda: _verify_backup_sync(
            in_path=in_path,
            password=password,
        )
    )
    await _audit_kdf_version(action="backup.verify", in_path=in_path, result=result)
    return result


__all__ = [
    "BackupError",
    "BackupNotAvailable",
    "create_backup",
    "restore_backup",
    "verify_backup",
]
