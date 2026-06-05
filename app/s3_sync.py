"""Write-only S3-compatible cross-machine sync (v1.x — backup destination only).

Persona is the source of truth; the configured S3/R2 bucket is a pure
backup target. The sync NEVER reads from S3 (no restore, no manifest
fetch) — that means a misconfigured bucket cannot corrupt local state,
and a third-party with read-only credentials cannot exfiltrate decrypted
data from S3 alone (everything is Fernet-encrypted before upload).

Format on S3
============

Two object kinds live under ``s3_sync_prefix``:

* ``persona-YYYY-MM-DD.db.enc`` — the nightly SQLite snapshot, produced
  by :meth:`sqlite3.Connection.backup` (online-backup API for a
  consistent read while the writer is live) and then Fernet-encrypted
  with a key derived from ``s3_sync_passphrase``.
* ``thumbnails/<relative-path>.enc`` — every thumbnail file under
  :attr:`Settings.thumbnails_dir`, individually Fernet-encrypted. The
  on-bucket layout mirrors the local layout one-to-one so a future
  human-driven restore can resolve a single file without downloading
  the entire archive.

Each thumbnail upload is gated by the previous run's mtime — a marker
row ``s3_sync_last_mtime`` in :func:`app.storage.repository.set_kv`
holds the largest ``st_mtime`` we have already uploaded, so a 200k-file
thumbnail tree does not re-upload itself every night. New runs walk
the directory once, skip files whose ``st_mtime`` is not greater than
the marker, and finally advance the marker to the new maximum.

Graceful degradation
====================

The sync function NEVER raises through to the caller. Three status
strings (returned in the ``status`` field of the result dict) cover
the non-OK paths:

* ``missing_dep`` — :mod:`boto3` and/or :mod:`cryptography` is not
  installed. The optional ``[s3]`` extra in :file:`pyproject.toml`
  pulls both in a future release; for now we just degrade.
* ``missing_config`` — at least one of bucket / access_key /
  secret_key / passphrase kv rows is empty.
* ``error`` — the upload itself raised (network, credentials, quota).
  ``error`` field on the result holds the exception string. The
  ``s3_sync_last_mtime`` marker is NOT advanced on failure so the
  next tick retries the same files.

Successful runs return ``status="ok"`` plus three counters
(``db_uploaded``, ``thumbnails_uploaded``, ``bytes_total``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypedDict

import anyio

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

if TYPE_CHECKING:
    from collections.abc import Iterable

log = get_logger("persona.s3_sync")


# ---------------------------------------------------------------------------
# Public TypedDict shape
# ---------------------------------------------------------------------------


class SyncResult(TypedDict, total=False):
    """Shape returned by :func:`sync_to_s3`.

    ``status`` is always present. The remaining fields are filled
    based on the branch — successful runs include the counters,
    error runs include ``error``.
    """

    status: str
    db_uploaded: int
    thumbnails_uploaded: int
    bytes_total: int
    error: str


# ---------------------------------------------------------------------------
# kv keys — must match the worker + the settings router.
# ---------------------------------------------------------------------------

_KV_BUCKET: Final[str] = "s3_sync_bucket"
_KV_PREFIX: Final[str] = "s3_sync_prefix"
_KV_ACCESS_KEY: Final[str] = "s3_sync_access_key"
_KV_SECRET_KEY: Final[str] = "s3_sync_secret_key"  # noqa: S105 — kv row NAME, not a credential
_KV_ENDPOINT: Final[str] = "s3_sync_endpoint_url"
_KV_PASSPHRASE: Final[str] = "s3_sync_passphrase"  # noqa: S105 — kv row NAME, not a credential
_KV_LAST_MTIME: Final[str] = "s3_sync_last_mtime"

# Fernet key derivation — PBKDF2-SHA256 with a stable salt. The bucket
# already enforces per-prefix isolation and the passphrase is never
# shipped off-box, so a stable salt is acceptable and lets us derive
# the same key on every sync without a kv round-trip.
_KDF_ITERATIONS: Final[int] = 600_000
_KDF_SALT: Final[bytes] = b"persona-s3-sync-v1"
_KDF_KEY_BYTES: Final[int] = 32


# ---------------------------------------------------------------------------
# Optional-dependency probes — `cryptography` and `boto3` are intentionally
# left out of the base lockfile (the bucket is a niche power-user feature).
# ---------------------------------------------------------------------------


def _have_cryptography() -> bool:
    try:
        from cryptography.fernet import Fernet  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def _have_boto3() -> bool:
    try:
        import boto3  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class _Config(TypedDict):
    bucket: str
    prefix: str
    access_key: str
    secret_key: str
    endpoint_url: str
    passphrase: str


async def _load_config() -> _Config | None:
    """Read all kv rows and return None when anything required is empty.

    Required fields: bucket, access_key, secret_key, passphrase. The
    prefix is optional (defaults to ``""`` — root of the bucket). The
    endpoint URL is optional (AWS S3 if absent; set it for R2/MinIO).
    """
    async with get_connection() as conn:
        bucket = (await get_kv(conn, _KV_BUCKET)) or ""
        prefix = (await get_kv(conn, _KV_PREFIX)) or ""
        access_key = (await get_kv(conn, _KV_ACCESS_KEY)) or ""
        secret_key = (await get_kv(conn, _KV_SECRET_KEY)) or ""
        endpoint = (await get_kv(conn, _KV_ENDPOINT)) or ""
        passphrase = (await get_kv(conn, _KV_PASSPHRASE)) or ""

    bucket = bucket.strip()
    access_key = access_key.strip()
    secret_key = secret_key.strip()
    passphrase = passphrase.strip()
    endpoint = endpoint.strip()
    # Normalise the prefix: strip whitespace + leading slash so we can
    # always join with a single ``/`` and never double-slash on S3.
    prefix = prefix.strip().lstrip("/").rstrip("/")

    if not bucket or not access_key or not secret_key or not passphrase:
        return None

    return _Config(
        bucket=bucket,
        prefix=prefix,
        access_key=access_key,
        secret_key=secret_key,
        endpoint_url=endpoint,
        passphrase=passphrase,
    )


# ---------------------------------------------------------------------------
# Fernet key derivation + encryption helpers
# ---------------------------------------------------------------------------


def _derive_fernet_key(passphrase: str) -> bytes:
    """Return a url-safe base64 32-byte Fernet key derived from passphrase.

    Fernet keys must be base64-encoded 32-byte values. We derive 32 raw
    bytes via PBKDF2 and base64-encode them in the URL-safe alphabet
    (Fernet's chosen alphabet).
    """
    raw = hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        _KDF_SALT,
        _KDF_ITERATIONS,
        dklen=_KDF_KEY_BYTES,
    )
    return base64.urlsafe_b64encode(raw)


def _encrypt_bytes(payload: bytes, fernet_key: bytes) -> bytes:
    """Encrypt ``payload`` with Fernet. Caller guarantees crypto is importable."""
    from cryptography.fernet import Fernet  # noqa: PLC0415 — optional dep

    # ``Fernet.encrypt`` is typed as returning ``bytes`` in stubs ≥ 41 but
    # ships as ``Any`` in older releases — wrap in ``bytes(...)`` so mypy
    # strict mode is happy across the range of versions the user may
    # have installed via the optional [s3] extra.
    return bytes(Fernet(fernet_key).encrypt(payload))


# ---------------------------------------------------------------------------
# SQLite snapshot dump (online backup API)
# ---------------------------------------------------------------------------


def _dump_sqlite_snapshot(db_path: Path, dest: Path) -> None:
    """Copy a live SQLite DB into ``dest`` via :meth:`Connection.backup`.

    The online-backup API yields a transactionally consistent snapshot
    even while another process is writing to ``db_path`` — that is the
    reason we don't ``shutil.copy``. WAL checkpoint runs first so the
    snapshot also includes uncheckpointed transactions.

    The destination file is overwritten if it already exists.
    """
    if not db_path.exists():
        msg = f"sqlite database not found: {db_path}"
        raise FileNotFoundError(msg)

    source = sqlite3.connect(str(db_path))
    try:
        source.execute("PRAGMA wal_checkpoint(FULL)")
        source.commit()
    finally:
        source.close()

    if dest.exists():
        dest.unlink()

    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


# ---------------------------------------------------------------------------
# S3 client wrapper
# ---------------------------------------------------------------------------


class _S3Uploader:
    """Thin wrapper around :mod:`boto3` so the upload loop stays readable.

    Lives only for the duration of one sync — we don't cache the client
    across runs because the kv configuration can change at any moment
    and a stale client would silently keep writing to the old bucket.
    """

    def __init__(self, *, config: _Config) -> None:
        # Lazy import: callers have already verified ``_have_boto3()``.
        import boto3  # noqa: PLC0415

        client_kwargs: dict[str, Any] = {
            "aws_access_key_id": config["access_key"],
            "aws_secret_access_key": config["secret_key"],
        }
        if config["endpoint_url"]:
            client_kwargs["endpoint_url"] = config["endpoint_url"]

        self._client = boto3.client("s3", **client_kwargs)
        self._bucket = config["bucket"]
        self._prefix = config["prefix"]

    def _key(self, suffix: str) -> str:
        """Join the configured prefix with ``suffix`` into a valid S3 key."""
        clean = suffix.lstrip("/")
        if not self._prefix:
            return clean
        return f"{self._prefix}/{clean}"

    def put(self, *, suffix: str, payload: bytes) -> int:
        """Upload ``payload`` under ``prefix/suffix``. Return bytes written."""
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key(suffix),
            Body=payload,
        )
        return len(payload)


# ---------------------------------------------------------------------------
# mtime marker — used to avoid re-uploading unchanged thumbnails
# ---------------------------------------------------------------------------


async def _read_last_mtime() -> float:
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_LAST_MTIME)
    if not raw:
        return 0.0
    try:
        return float(raw.strip())
    except ValueError:
        return 0.0


async def _write_last_mtime(value: float) -> None:
    async with get_connection() as conn:
        await set_kv(conn, _KV_LAST_MTIME, repr(value))


# ---------------------------------------------------------------------------
# Sync core — runs entirely in a worker thread so blocking boto3 calls
# don't stall the event loop.
# ---------------------------------------------------------------------------


def _iter_thumbnails(root: Path) -> Iterable[Path]:
    """Yield every file under ``root``. Returns empty iterator on missing root."""
    if not root.exists():
        return
    for entry in root.rglob("*"):
        if entry.is_file():
            yield entry


def _sync_sync(*, config: _Config, last_mtime: float) -> tuple[SyncResult, float]:
    """Blocking core called via ``anyio.to_thread.run_sync``.

    Returns ``(result, new_last_mtime)``. The caller is responsible for
    persisting the new mtime via :func:`_write_last_mtime` — that way
    a failure after the upload phase still leaves a usable marker but
    a failure in the middle does not.
    """
    fernet_key = _derive_fernet_key(config["passphrase"])
    uploader = _S3Uploader(config=config)
    bytes_total = 0
    db_uploaded = 0
    thumbnails_uploaded = 0
    settings = get_settings()

    # --- DB snapshot --------------------------------------------------
    today_iso = datetime.now().astimezone().date().isoformat()
    with tempfile.TemporaryDirectory(prefix="persona-s3-") as tmp:
        snapshot_path = Path(tmp) / "persona-snapshot.db"
        _dump_sqlite_snapshot(settings.db_path, snapshot_path)
        plaintext = snapshot_path.read_bytes()

    encrypted = _encrypt_bytes(plaintext, fernet_key)
    written = uploader.put(
        suffix=f"persona-{today_iso}.db.enc",
        payload=encrypted,
    )
    bytes_total += written
    db_uploaded = 1
    log.info(
        "s3_sync.db.uploaded",
        bytes=written,
        plaintext_bytes=len(plaintext),
        day=today_iso,
    )

    # --- Thumbnails ---------------------------------------------------
    thumbs_root = settings.thumbnails_dir
    new_max_mtime = last_mtime

    for thumb in _iter_thumbnails(thumbs_root):
        try:
            stat = thumb.stat()
        except OSError as exc:
            log.warning("s3_sync.thumbnail.stat_failed", path=str(thumb), error=str(exc))
            continue
        if stat.st_mtime <= last_mtime:
            continue

        try:
            payload = thumb.read_bytes()
        except OSError as exc:
            log.warning("s3_sync.thumbnail.read_failed", path=str(thumb), error=str(exc))
            continue

        try:
            relative = thumb.relative_to(thumbs_root).as_posix()
        except ValueError:
            relative = thumb.name

        encrypted_blob = _encrypt_bytes(payload, fernet_key)
        written = uploader.put(
            suffix=f"thumbnails/{relative}.enc",
            payload=encrypted_blob,
        )
        bytes_total += written
        thumbnails_uploaded += 1
        new_max_mtime = max(new_max_mtime, stat.st_mtime)

    result: SyncResult = {
        "status": "ok",
        "db_uploaded": db_uploaded,
        "thumbnails_uploaded": thumbnails_uploaded,
        "bytes_total": bytes_total,
    }
    return result, new_max_mtime


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def sync_to_s3() -> SyncResult:
    """Run one full sync. Never raises — always returns a SyncResult.

    Behaviour:

    1. If :mod:`boto3` or :mod:`cryptography` is missing →
       ``status="missing_dep"``.
    2. If any required kv row is unset → ``status="missing_config"``.
    3. Otherwise: dump SQLite via online-backup API, walk thumbnails,
       upload encrypted files with new mtimes only, advance the
       mtime marker, return ``status="ok"``.
    4. Any exception during upload → ``status="error"`` with the
       message in ``error``. The marker is NOT advanced.
    """
    if not _have_boto3() or not _have_cryptography():
        log.warning(
            "s3_sync.skipped",
            reason="missing_dep",
            boto3=_have_boto3(),
            cryptography=_have_cryptography(),
        )
        return {"status": "missing_dep"}

    config = await _load_config()
    if config is None:
        log.info("s3_sync.skipped", reason="missing_config")
        return {"status": "missing_config"}

    last_mtime = await _read_last_mtime()
    log.info(
        "s3_sync.start",
        bucket=config["bucket"],
        prefix=config["prefix"],
        endpoint=config["endpoint_url"] or "aws",
        last_mtime=last_mtime,
    )

    try:
        result, new_mtime = await anyio.to_thread.run_sync(
            lambda: _sync_sync(config=config, last_mtime=last_mtime)
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("s3_sync.failed", error=str(exc))
        return {"status": "error", "error": str(exc)}

    # Only advance the marker after a successful upload phase. A
    # partial-success run (e.g. crash midway) leaves the marker alone
    # so the next tick re-uploads everything the failed run missed.
    if new_mtime > last_mtime:
        await _write_last_mtime(new_mtime)

    # Re-attach environment-aware fields the caller may want to log.
    log.info(
        "s3_sync.done",
        db_uploaded=result.get("db_uploaded", 0),
        thumbnails_uploaded=result.get("thumbnails_uploaded", 0),
        bytes_total=result.get("bytes_total", 0),
    )
    return result


def _kv_keys() -> dict[str, str]:
    """Expose the kv key constants for the settings router (single source).

    Both the worker and the settings page also pin these names as
    module-level constants, but going through one helper keeps the kv
    schema documented in exactly one place.
    """
    return {
        "bucket": _KV_BUCKET,
        "prefix": _KV_PREFIX,
        "access_key": _KV_ACCESS_KEY,
        "secret_key": _KV_SECRET_KEY,
        "endpoint_url": _KV_ENDPOINT,
        "passphrase": _KV_PASSPHRASE,
    }


__all__ = [
    "SyncResult",
    "_kv_keys",
    "sync_to_s3",
]
