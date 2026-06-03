"""Diagnostics export bundle — single ZIP suitable for a bug-report attachment.

The companion to :mod:`app.diagnostics`. ``doctor`` answers "what is
broken right now?"; this module answers "give me everything an
investigator needs to reproduce the failure on their own bench,
without leaking my notes/screenshots/OCR".

Contents of the resulting ``persona-diag-<DATE>.zip``::

    version.txt              — Persona / Python / platform string
    doctor.json              — full ``run_doctor`` battery from v0.22
    routes.txt               — every registered HTTP route, sorted
    settings_redacted.json   — ``export_settings_json`` with API keys removed
    migrations.txt           — list of migration files present on disk
    recent_audit.json        — newest 50 ``audit_log`` rows (action/ts only)

Hard rules — these MUST stay true for every future addition:

* **No user content, ever.** No screenshots, no OCR text, no notes
  (encrypted or otherwise), no embeddings, no vault rows. The whole
  point of the bundle is "safe to attach to a public bug tracker".
* **Secrets are stripped twice.** ``export_settings_json`` already
  strips ``webhook.secret``; on top of that we walk every value of
  ``kv_setting`` and blank out any key that *looks* like an API key,
  token, secret or password. Defence in depth — a future migration
  that adds a new sensitive column should not silently leak.
* **All blocking IO** (``zipfile``, ``Path.read_text``, ``stat``)
  runs inside :func:`anyio.to_thread.run_sync` so the async route
  layer keeps serving other requests while the bundle is built.
"""

from __future__ import annotations

import json
import platform
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import anyio

from app import __version__
from app.audit import list_recent
from app.diagnostics import run_doctor
from app.logging_setup import get_logger
from app.settings_backup import export_settings_json

if TYPE_CHECKING:
    from collections.abc import Iterable

log = get_logger("persona.diag")

# Schema marker — bumped together with the layout above so an older
# triage script can refuse a bundle it does not understand.
SCHEMA_VERSION: Final[str] = "persona-diag-1"

# Newest N audit rows to include. The number is small on purpose: bug
# reports want recent context, not the full forensic trail.
_AUDIT_TAIL: Final[int] = 50

# Compression level — pinned for the same reason the archive bundle pins
# it: a future stdlib default change cannot silently bloat the artefact.
_COMPRESS_LEVEL: Final[int] = 6

# Substring needles (lowercased) that flag a ``kv_settings`` key as
# sensitive and therefore get its value blanked out. Match is partial
# so e.g. ``byo_api_key``, ``slack_webhook_token`` and ``smtp_password``
# all hit. Bumped whenever a new sensitive setting is introduced.
_SENSITIVE_KEY_NEEDLES: Final[tuple[str, ...]] = (
    "api_key",
    "api_token",
    "secret",
    "password",
    "passphrase",
    "private_key",
    "access_token",
    "refresh_token",
    "bearer",
)

# Sentinel written in place of a stripped value so a reader can tell
# "blanked for safety" from "the user actually left it empty".
_REDACTED: Final[str] = "***REDACTED***"

# Migrations live alongside the schema; we list the files on disk
# because ``init_database`` applies every ``.sql`` it finds (idempotent)
# rather than tracking applied versions in a dedicated table.
_MIGRATIONS_DIR: Final[Path] = Path(__file__).parent / "storage" / "migrations"


def _version_blob() -> str:
    """Return a short ``key: value`` block describing the runtime.

    Plain text rather than JSON so a human triaging the ZIP sees the
    important lines instantly in any text viewer. Avoids any field that
    could carry user-identifying information (no hostname, no username,
    no environment variables).
    """
    py = sys.version_info
    return (
        f"persona_version: {__version__}\n"
        f"schema:          {SCHEMA_VERSION}\n"
        f"python_version:  {py.major}.{py.minor}.{py.micro}\n"
        f"python_impl:     {platform.python_implementation()}\n"
        f"platform:        {platform.platform()}\n"
        f"machine:         {platform.machine()}\n"
        f"system:          {platform.system()} {platform.release()}\n"
    )


def _is_sensitive_key(key: str) -> bool:
    """Return True if ``key`` matches any of :data:`_SENSITIVE_KEY_NEEDLES`."""
    lowered = key.lower()
    return any(needle in lowered for needle in _SENSITIVE_KEY_NEEDLES)


def _redact_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` with API-key-shaped ``kv_setting`` values blanked.

    We mutate a deep-ish copy rather than the original because the
    caller (``export_settings_json``) is also used by the regular
    settings backup route — never let the diag pipeline taint that.
    """
    # Round-tripping through JSON gives us a deep copy AND a guarantee
    # that the result is JSON-encodable. ``cast`` is the lesser evil
    # over a ``copy.deepcopy`` that would leave non-JSON exotic types in
    # place and explode later in :func:`_write_zip`.
    redacted: dict[str, Any] = json.loads(json.dumps(payload))
    tables = redacted.get("tables")
    if not isinstance(tables, dict):
        return redacted
    kv_rows = tables.get("kv_setting")
    if not isinstance(kv_rows, list):
        return redacted

    for row in kv_rows:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if isinstance(key, str) and _is_sensitive_key(key):
            row["value"] = _REDACTED
    return redacted


def _migrations_blob(files: Iterable[Path]) -> str:
    """Render the migration list as a sorted, one-per-line plain text block."""
    names = sorted(p.name for p in files)
    if not names:
        return "(no migrations found)\n"
    return "\n".join(names) + "\n"


def _routes_blob() -> str:
    """List every registered HTTP route as ``METHODS  PATH`` lines.

    Imported lazily so the bundle module stays usable from contexts
    (CLI, tests) that have not paid the cost of constructing the full
    FastAPI ``app``. The CLI ``diagnostics-bundle`` subcommand depends
    on this lazy import to keep its startup snappy.
    """
    try:
        # Local import — ``app.web.main`` builds the full FastAPI ``app``
        # at import time, so importing it eagerly at module top would
        # drag every route module into every consumer of this helper
        # (CLI, tests) and cause a circular import via this very route.
        # ``BaseException`` is too broad, but ``Exception`` is the right
        # net here: a buggy route module raises ``FastAPIError`` (not an
        # ``ImportError``) at import time, and the whole point of the
        # bundle is to keep working when the install is partly broken.
        from app.web.main import app as fastapi_app  # noqa: PLC0415
    except Exception as exc:
        log.warning("diag.routes.import_failed", error=str(exc))
        return f"(routes unavailable: {exc})\n"

    lines: list[str] = []
    for route in fastapi_app.routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            continue
        methods_raw = getattr(route, "methods", None) or set()
        # Filter ``HEAD`` — FastAPI adds it automatically for every GET
        # and it doubles the line count without adding signal.
        methods = sorted(m for m in methods_raw if m != "HEAD")
        label = ",".join(methods) if methods else "-"
        lines.append(f"{label:<12} {path}")
    lines.sort()
    return "\n".join(lines) + ("\n" if lines else "")


def _audit_blob(rows: list[dict[str, Any]]) -> bytes:
    """Encode the recent-audit slice as pretty JSON bytes."""
    payload = {
        "schema": SCHEMA_VERSION,
        "limit": _AUDIT_TAIL,
        "rows": rows,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _write_zip(
    *,
    output_path: Path,
    version_blob: str,
    doctor_blob: bytes,
    routes_blob: str,
    settings_blob: bytes,
    migrations_blob: str,
    audit_blob: bytes,
) -> int:
    """Synchronous worker — assemble the ZIP and return its size in bytes.

    Runs inside :func:`anyio.to_thread.run_sync` because ``zipfile`` is
    blocking IO. Returns the file size so the caller can report it.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=_COMPRESS_LEVEL,
    ) as zf:
        zf.writestr("version.txt", version_blob)
        zf.writestr("doctor.json", doctor_blob)
        zf.writestr("routes.txt", routes_blob)
        zf.writestr("settings_redacted.json", settings_blob)
        zf.writestr("migrations.txt", migrations_blob)
        zf.writestr("recent_audit.json", audit_blob)
    return output_path.stat().st_size


async def build_diag_bundle(output_path: Path | None = None) -> dict[str, Any]:
    """Build a diagnostics ``.zip`` at ``output_path`` and return a summary.

    Args:
        output_path: Destination path. Required — callers that want a
            temporary file should use :mod:`tempfile` and pass the
            result in. ``None`` raises :class:`ValueError` so the
            artefact never silently lands in cwd (mirrors the
            :func:`app.archive_bundle.build_archive` contract).

    Returns:
        ``{"status": "ok", "path": str, "size_bytes": int}``. The
        shape is intentionally minimal so the route layer can stream
        the file straight back without consulting another data source.

    Raises:
        ValueError: When ``output_path`` is ``None``.
    """
    if output_path is None:
        msg = "output_path is required (use tempfile if you don't have a target)"
        raise ValueError(msg)

    # 1. Gather everything that needs the event loop *first*, then hand
    #    the assembled blobs to a single ``run_sync`` call. Keeps the
    #    blocking-IO surface to one syscall stack.
    doctor_rows = await run_doctor()
    settings_payload = await export_settings_json()
    audit_rows_typed = await list_recent(limit=_AUDIT_TAIL)
    # ``AuditRow`` is a ``TypedDict``; ``json.dumps`` accepts it as-is
    # but the type checker is happier with a plain ``dict`` copy.
    audit_rows: list[dict[str, Any]] = [dict(row) for row in audit_rows_typed]

    redacted_settings = _redact_settings(settings_payload)

    version_blob = _version_blob()
    doctor_blob = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "results": doctor_rows,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    routes_blob = _routes_blob()
    settings_blob = json.dumps(
        redacted_settings, ensure_ascii=False, indent=2
    ).encode("utf-8")
    migration_files = (
        list(_MIGRATIONS_DIR.glob("*.sql")) if _MIGRATIONS_DIR.exists() else []
    )
    migrations_blob = _migrations_blob(migration_files)
    audit_blob = _audit_blob(audit_rows)

    size_bytes = await anyio.to_thread.run_sync(
        lambda: _write_zip(
            output_path=output_path,
            version_blob=version_blob,
            doctor_blob=doctor_blob,
            routes_blob=routes_blob,
            settings_blob=settings_blob,
            migrations_blob=migrations_blob,
            audit_blob=audit_blob,
        )
    )

    log.info(
        "diag.bundle.ok",
        path=str(output_path),
        size_bytes=size_bytes,
        doctor_checks=len(doctor_rows),
        audit_rows=len(audit_rows),
        migrations=len(migration_files),
    )

    return {
        "status": "ok",
        "path": str(output_path),
        "size_bytes": size_bytes,
    }


__all__ = ["SCHEMA_VERSION", "build_diag_bundle"]
