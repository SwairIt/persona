"""JSON export / import of every preference table — for migration between machines.

Persona stores user-tweakable settings across many small tables (kv_settings,
redaction_rule, auto_collection, …). Migrating a workstation manually is a
pain: dump each table, re-insert each row, hope the schemas match. This
module bundles every preference table into a single JSON blob that
:func:`export_settings_json` produces and :func:`import_settings_json`
consumes.

Hard rules — these MUST be respected for every future addition:

* No screenshots, OCR text, embeddings, or audit logs — preferences only.
* The ``secret`` column of every webhook is stripped before export. The
  recipient must regenerate signing keys after import.
* The ``kv_vault`` ciphertext rows are NEVER exported. Anyone with the
  ciphertext + a passphrase guess can attempt decryption offline, so
  shipping the blob to another machine breaks the "encryption at rest"
  promise. Vault rows must be re-entered manually.
* Every SQL is parametrised. No f-string interpolation against user data
  even though this is a single-user local app.

The schema field of the resulting blob is bumped whenever a table is
added / renamed so older clients can refuse imports they don't
understand instead of silently dropping rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.settings_backup")

# Bump whenever a table is added / renamed / removed. Imports check this
# value and refuse blobs they don't recognise to avoid silent data loss.
SCHEMA_VERSION: Final[str] = "persona-settings-1"

# Logical name (used in the JSON payload) → real SQL table.
# The spec uses ``kv_setting`` / ``webhook`` (singular) but the actual
# schema is ``kv_settings`` / ``webhooks``; the export uses the spec
# names as JSON keys so the format stays stable even if a future
# migration renames the underlying table.
_TABLE_MAP: Final[dict[str, str]] = {
    "kv_setting": "kv_settings",
    "redaction_rule": "redaction_rule",
    "auto_collection": "auto_collection",
    "ocr_skip_app": "ocr_skip_app",
    "ocr_phrase_tag": "ocr_phrase_tag",
    "saved_search": "saved_search",
    "note_template": "note_template",
    "app_overrides": "app_capture_overrides",
    "webhook": "webhooks",
    "quiet_hours": "quiet_hours",
}

# Columns to drop on export — protects sensitive material that must not
# travel between machines. Keyed by the *real* SQL table name.
_SENSITIVE_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "webhooks": frozenset({"secret"}),
}


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    """Return ``True`` if ``table`` is present in ``sqlite_master``."""
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )
    row = await cursor.fetchone()
    return row is not None


async def _table_columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    """Return the ordered column list for ``table`` via PRAGMA table_info."""
    # PRAGMA does not accept bind parameters; the table name comes from
    # the hard-coded ``_TABLE_MAP`` and is never user input, so quoting
    # it inline is safe. We still validate against ``sqlite_master``
    # above before reaching this path.
    cursor = await conn.execute(f'PRAGMA table_info("{table}")')
    rows = await cursor.fetchall()
    return [str(row["name"]) for row in rows]


async def _dump_table(
    conn: aiosqlite.Connection,
    real_table: str,
    drop_columns: frozenset[str],
) -> list[dict[str, Any]]:
    """Return every row of ``real_table`` as ``[{column: value, …}, …]``.

    Columns named in ``drop_columns`` are stripped from the output — the
    webhook ``secret`` column is the canonical example. ``BLOB`` values
    are dropped to ``None`` so the JSON encoder doesn't choke on bytes
    (the preference tables don't actually use BLOBs today, but the
    guard keeps the function honest if one is added later).
    """
    columns = await _table_columns(conn, real_table)
    if not columns:
        return []

    select_columns = [c for c in columns if c not in drop_columns]
    if not select_columns:
        return []

    quoted = ", ".join(f'"{c}"' for c in select_columns)
    cursor = await conn.execute(f'SELECT {quoted} FROM "{real_table}"')  # noqa: S608
    rows = await cursor.fetchall()

    payload: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for col in select_columns:
            value = row[col]
            if isinstance(value, bytes):
                # Preference tables never store BLOBs intentionally;
                # if one slips in (corrupt row, future migration) we
                # refuse to leak it rather than base64-encode silently.
                item[col] = None
            else:
                item[col] = value
        payload.append(item)
    return payload


async def export_settings_json() -> dict[str, Any]:
    """Return a JSON-serialisable dict of every preference table.

    The result is a flat document::

        {
            "schema": "persona-settings-1",
            "generated_at": "2026-06-02T12:34:56.789012+00:00",
            "tables": {
                "kv_setting": [{"key": "theme", "value": "dark", ...}, ...],
                ...
            }
        }

    Sensitive columns (``webhook.secret``) are stripped before they
    enter the dict. Missing tables (e.g. an older DB that hasn't run
    the matching migration) appear as ``[]`` rather than disappearing
    so the receiving side can tell "no rows" from "table absent".
    """
    tables: dict[str, list[dict[str, Any]]] = {}
    async with get_connection() as conn:
        for logical, real in _TABLE_MAP.items():
            if not await _table_exists(conn, real):
                log.warning(
                    "settings_backup.export.table_missing",
                    logical=logical,
                    real=real,
                )
                tables[logical] = []
                continue
            drop = _SENSITIVE_COLUMNS.get(real, frozenset())
            tables[logical] = await _dump_table(conn, real, drop)

    total_rows = sum(len(rows) for rows in tables.values())
    log.info(
        "settings_backup.export.ok",
        tables=len(tables),
        rows=total_rows,
    )
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "tables": tables,
    }


def _validate_payload(data: Any) -> dict[str, list[dict[str, Any]]]:
    """Sanity-check an incoming blob and return its ``tables`` section.

    Raises :class:`ValueError` on any structural problem so the route
    layer can return a clean 400 instead of an opaque 500.
    """
    if not isinstance(data, dict):
        msg = "expected a JSON object at the top level"
        raise ValueError(msg)
    schema = data.get("schema")
    if schema != SCHEMA_VERSION:
        msg = f"unsupported schema {schema!r} (expected {SCHEMA_VERSION!r})"
        raise ValueError(msg)
    tables_raw = data.get("tables")
    if not isinstance(tables_raw, dict):
        msg = "'tables' must be an object"
        raise ValueError(msg)

    tables: dict[str, list[dict[str, Any]]] = {}
    for logical, rows in tables_raw.items():
        if not isinstance(logical, str) or logical not in _TABLE_MAP:
            log.warning("settings_backup.import.unknown_table", logical=logical)
            continue
        if not isinstance(rows, list):
            msg = f"table {logical!r} must be a list"
            raise ValueError(msg)
        normalised: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                msg = f"row in {logical!r} must be an object"
                raise ValueError(msg)
            normalised.append(dict(row))
        tables[logical] = normalised
    return tables


async def _insert_rows(
    conn: aiosqlite.Connection,
    real_table: str,
    rows: list[dict[str, Any]],
    *,
    or_ignore: bool,
) -> int:
    """Insert ``rows`` into ``real_table``; return the number written.

    Columns named in :data:`_SENSITIVE_COLUMNS` are also dropped on
    *import* — if a malicious blob tries to smuggle a webhook secret
    in, we silently strip it rather than honour the value.
    """
    if not rows:
        return 0

    valid_columns = set(await _table_columns(conn, real_table))
    if not valid_columns:
        return 0
    drop = _SENSITIVE_COLUMNS.get(real_table, frozenset())

    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    written = 0
    for row in rows:
        cols = [c for c in row if c in valid_columns and c not in drop]
        if not cols:
            continue
        placeholders = ", ".join("?" for _ in cols)
        quoted = ", ".join(f'"{c}"' for c in cols)
        values = [row[c] for c in cols]
        await conn.execute(
            f'{verb} INTO "{real_table}" ({quoted}) VALUES ({placeholders})',
            values,
        )
        written += 1
    return written


async def import_settings_json(data: Any, merge: bool = True) -> dict[str, int]:
    """Insert every row from ``data`` into the matching preference tables.

    Parameters
    ----------
    data
        The dict produced by :func:`export_settings_json`. Must declare
        the current :data:`SCHEMA_VERSION` or :class:`ValueError` is
        raised before any write happens.
    merge
        * ``True`` (default) — ``INSERT OR IGNORE`` so existing rows
          win. Safe to re-run; useful for "pull settings from machine A
          without losing local tweaks".
        * ``False`` — truncate the destination table first, then
          ``INSERT``. Destructive; use when restoring a known-good
          snapshot to a fresh install.

    Returns a dict ``{logical_table: rows_written}`` for callers that
    want to surface a summary to the user.
    """
    tables = _validate_payload(data)
    written: dict[str, int] = {}

    async with get_connection() as conn:
        try:
            await conn.execute("BEGIN")
            for logical, rows in tables.items():
                real = _TABLE_MAP[logical]
                if not await _table_exists(conn, real):
                    log.warning(
                        "settings_backup.import.table_missing",
                        logical=logical,
                        real=real,
                    )
                    written[logical] = 0
                    continue
                if not merge:
                    await conn.execute(f'DELETE FROM "{real}"')  # noqa: S608
                written[logical] = await _insert_rows(
                    conn, real, rows, or_ignore=merge
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    total = sum(written.values())
    log.info(
        "settings_backup.import.ok",
        merge=merge,
        tables=len(written),
        rows=total,
    )
    return written


__all__ = [
    "SCHEMA_VERSION",
    "export_settings_json",
    "import_settings_json",
]
