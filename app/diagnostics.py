"""System diagnostics — a battery of fast checks for triage.

Each check returns ``{"name": str, "status": "pass"|"warn"|"fail", "detail": str}``.

The collection is exposed via :func:`run_doctor`, which the CLI ``persona-cli doctor``
subcommand and the ``/doctor`` web page both consume.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.settings import get_settings
from app.storage.db import get_connection

CheckResult = dict[str, str]


def _ok(name: str, detail: str) -> CheckResult:
    return {"name": name, "status": "pass", "detail": detail}


def _warn(name: str, detail: str) -> CheckResult:
    return {"name": name, "status": "warn", "detail": detail}


def _fail(name: str, detail: str) -> CheckResult:
    return {"name": name, "status": "fail", "detail": detail}


def _check_python_version() -> CheckResult:
    """Warn if Python is older than 3.12."""
    version = sys.version_info
    label = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) < (3, 12):
        return _warn("python_version", f"Python {label} (< 3.12 — upgrade recommended)")
    return _ok("python_version", f"Python {label}")


def _check_sqlite_version() -> CheckResult:
    """Fail if SQLite's FTS5 extension is missing."""
    version = sqlite3.sqlite_version
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
        finally:
            probe.close()
    except sqlite3.OperationalError as exc:
        return _fail("sqlite_version", f"SQLite {version}; FTS5 missing: {exc}")
    return _ok("sqlite_version", f"SQLite {version}, FTS5 OK")


def _check_data_dir_writable() -> CheckResult:
    """Fail if ``data/.probe`` cannot be written then removed."""
    settings = get_settings()
    data_dir: Path = settings.data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return _fail("data_dir_writable", f"{data_dir}: {exc}")
    return _ok("data_dir_writable", f"{data_dir} is writable")


def _check_db_path_exists_and_readable() -> CheckResult:
    """Fail if the SQLite DB file is missing or unreadable."""
    settings = get_settings()
    db_path: Path = settings.db_path
    if not db_path.exists():
        return _fail("db_path_exists_and_readable", f"missing: {db_path}")
    try:
        with db_path.open("rb") as handle:
            handle.read(16)
    except OSError as exc:
        return _fail("db_path_exists_and_readable", f"{db_path}: {exc}")
    size_mb = db_path.stat().st_size / (1024 * 1024)
    return _ok("db_path_exists_and_readable", f"{db_path} ({size_mb:.1f} MB)")


async def _check_db_integrity() -> CheckResult:
    """Run ``PRAGMA integrity_check`` and report the verdict."""
    try:
        async with get_connection() as conn:
            cursor = await conn.execute("PRAGMA integrity_check")
            row = await cursor.fetchone()
    except (aiosqlite.Error, OSError) as exc:
        return _fail("db_integrity", f"integrity_check raised: {exc}")
    verdict = "" if row is None else str(row[0])
    if verdict.lower() == "ok":
        return _ok("db_integrity", "PRAGMA integrity_check = ok")
    return _fail("db_integrity", f"PRAGMA integrity_check = {verdict!r}")


def _check_tesseract_available() -> CheckResult:
    """Pass with version if Tesseract is reachable, warn otherwise."""
    from app.ocr import probe_tesseract

    settings = get_settings()
    probe = probe_tesseract(settings.tesseract_path)
    if probe.available and probe.version:
        binary = str(probe.binary_path) if probe.binary_path else "?"
        return _ok("tesseract_available", f"v{probe.version} at {binary}")
    detail = probe.error or "binary not found"
    return _warn("tesseract_available", detail)


def _check_embeddings_lib_available() -> CheckResult:
    """Warn only when embeddings are enabled but fastembed cannot import."""
    settings = get_settings()
    if not settings.embeddings_enabled:
        return _ok("embeddings_lib_available", "embeddings disabled in settings")
    try:
        import fastembed  # noqa: F401
    except ImportError as exc:
        return _warn(
            "embeddings_lib_available",
            f"embeddings_enabled but fastembed not importable: {exc}",
        )
    return _ok("embeddings_lib_available", "fastembed importable")


def _check_byo_llm_configured() -> CheckResult:
    """Warn if features that need a BYO LLM are on but the API key/provider is blank."""
    settings = get_settings()
    needs_llm = settings.auto_digest_enabled
    if not needs_llm:
        return _ok("byo_llm_configured", "no LLM-requiring features enabled")
    missing: list[str] = []
    if not settings.byo_api_key:
        missing.append("PERSONA_BYO_API_KEY")
    if not settings.byo_api_provider:
        missing.append("PERSONA_BYO_API_PROVIDER")
    if missing:
        return _warn(
            "byo_llm_configured",
            f"auto_digest_enabled but missing: {', '.join(missing)}",
        )
    return _ok(
        "byo_llm_configured",
        f"provider: {settings.byo_api_provider}",
    )


def _check_disk_free() -> CheckResult:
    """Warn if <500MB free, fail if <100MB free on the disk holding ``data_dir``."""
    settings = get_settings()
    target: Path = settings.data_dir
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return _warn("disk_free", f"could not stat {target}: {exc}")
    free_mb = usage.free / (1024 * 1024)
    detail = f"{free_mb:.0f} MB free on {target.anchor or target}"
    if free_mb < 100:
        return _fail("disk_free", detail + " (< 100MB)")
    if free_mb < 500:
        return _warn("disk_free", detail + " (< 500MB)")
    return _ok("disk_free", detail)


def _dir_size_bytes(path: Path) -> int:
    """Sum the byte sizes of every regular file under ``path``."""
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _check_thumbnails_dir_size() -> CheckResult:
    """Informational — always pass; report the thumbnails directory size in MB."""
    settings = get_settings()
    target: Path = settings.thumbnails_dir
    total_bytes = _dir_size_bytes(target)
    size_mb = total_bytes / (1024 * 1024)
    return _ok("thumbnails_dir_size", f"{size_mb:.1f} MB at {target}")


async def _check_capture_loop_recent() -> CheckResult:
    """Warn if no captures in the last 24h — but only if any rows exist at all."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    try:
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots")
            total_row = await cursor.fetchone()
            total = int(total_row["n"]) if total_row else 0

            if total == 0:
                return _ok("capture_loop_recent", "no screenshots yet — fresh install")

            cursor = await conn.execute(
                "SELECT MAX(captured_at) AS last FROM screenshots"
            )
            last_row = await cursor.fetchone()
    except (aiosqlite.Error, OSError) as exc:
        return _warn("capture_loop_recent", f"query failed: {exc}")

    last_value = last_row["last"] if last_row else None
    if not last_value:
        return _warn("capture_loop_recent", "no captured_at on any screenshot")
    try:
        last_dt = datetime.fromisoformat(str(last_value).replace("Z", "+00:00"))
    except ValueError:
        return _warn("capture_loop_recent", f"unparseable timestamp: {last_value!r}")
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    label = last_dt.strftime("%Y-%m-%d %H:%M UTC")
    if last_dt < cutoff:
        delta = datetime.now(timezone.utc) - last_dt
        hours = int(delta.total_seconds() // 3600)
        return _warn(
            "capture_loop_recent",
            f"last capture {hours}h ago ({label}) — capture loop may be stopped",
        )
    return _ok("capture_loop_recent", f"last capture at {label}")


def _check_schema_version() -> CheckResult:
    """Count how many migration files exist on disk."""
    migrations_dir = Path(__file__).parent / "storage" / "migrations"
    if not migrations_dir.exists():
        return _warn("schema_version", f"{migrations_dir} does not exist")
    files = sorted(migrations_dir.glob("*.sql"))
    latest = files[-1].name if files else "(none)"
    return _ok("schema_version", f"{len(files)} migration file(s); latest: {latest}")


async def run_doctor() -> list[CheckResult]:
    """Run the full diagnostic battery and return one row per check."""
    results: list[CheckResult] = []
    results.append(_check_python_version())
    results.append(_check_sqlite_version())
    results.append(_check_data_dir_writable())
    results.append(_check_db_path_exists_and_readable())
    results.append(await _check_db_integrity())
    results.append(_check_tesseract_available())
    results.append(_check_embeddings_lib_available())
    results.append(_check_byo_llm_configured())
    results.append(_check_disk_free())
    results.append(_check_thumbnails_dir_size())
    results.append(await _check_capture_loop_recent())
    results.append(_check_schema_version())
    return results


__all__ = ["CheckResult", "run_doctor"]
