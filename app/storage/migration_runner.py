"""Transactional, checksum-verified SQLite schema migrations.

This module deliberately contains no application startup logic.  It receives an
already-open connection, takes the SQLite writer lock and either:

* installs a fresh database;
* records a reviewed baseline for a legacy database already at schema head; or
* applies only migration files missing from the ledger.

The old runner replayed every file on every process start.  That was especially
dangerous for FTS rebuilds and table-copy migrations, and it also overwrote
operator-edited configuration.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite

from app.logging_setup import get_logger

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger("persona.storage.migrations")

_MIGRATION_NAME_RE = re.compile(r"^(?P<order>\d{3})_[A-Za-z0-9_.-]+\.sql$")
_ALTER_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\b.+\bADD\s+(?:COLUMN\s+)?",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS\b)",
    re.IGNORECASE | re.DOTALL,
)
_VEC_MIGRATIONS = frozenset({"186_vec_memory.sql", "190_vec_screenshot.sql"})
# The last production migration released before the append-only ledger
# existed. A ledger-less installation may be proven at exactly this boundary,
# baselined through it, and then upgraded normally. Never move this boundary:
# future legacy compatibility needs a new reviewed boundary, not history edits.
_LEGACY_LEDGER_BOUNDARY_ORDER = 203

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    migration_order INTEGER NOT NULL,
    name TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    applied_at TEXT,
    duration_ms INTEGER,
    error TEXT,
    is_baseline INTEGER NOT NULL DEFAULT 0 CHECK (is_baseline IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_migration_order
    ON schema_migration(migration_order);
CREATE INDEX IF NOT EXISTS idx_schema_migration_status
    ON schema_migration(status);
CREATE TABLE IF NOT EXISTS schema_capability (
    name TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied', 'unavailable', 'failed')),
    checked_at TEXT NOT NULL,
    applied_at TEXT,
    error TEXT
);
"""


class MigrationError(RuntimeError):
    """Base class for migration failures that must block application startup."""


class MigrationChecksumError(MigrationError):
    """An applied migration file changed or moved."""


class MigrationFailedState(MigrationError):
    """A previous migration attempt failed and requires operator review."""


class LegacySchemaError(MigrationError):
    """A ledger-less database cannot be proven to be at the reviewed schema head."""


class _MigrationBodyError(MigrationError):
    def __init__(self, migration: Migration, cause: BaseException) -> None:
        self.migration = migration
        self.cause = cause
        super().__init__(f"{migration.name}: {cause}")


@dataclass(frozen=True, slots=True)
class Migration:
    order: int
    name: str
    checksum: str
    sql: str


@dataclass(frozen=True, slots=True)
class _CapabilityResult:
    name: str
    checksum: str
    status: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _IndexShape:
    name: str
    unique: int
    partial: int
    columns: tuple[tuple[str, int, str, int], ...]
    definition_checksum: str


@dataclass(frozen=True, slots=True)
class _SchemaObjectShape:
    kind: str
    definition_checksum: str
    columns: tuple[tuple[str, str, int, str, int, int], ...]
    foreign_keys: tuple[tuple[str, str, str, str, str, str], ...]
    indexes: tuple[_IndexShape, ...]
    triggers: tuple[tuple[str, str], ...]


def _checksum(sql: str) -> str:
    """Return a cross-platform checksum (Git may materialise CRLF on Windows)."""
    normalized = sql.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """Load and validate the ordered migration manifest."""
    migrations: list[Migration] = []
    seen_orders: set[int] = set()
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME_RE.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"Invalid migration filename: {path.name}")
        order = int(match.group("order"))
        if order in seen_orders:
            raise MigrationError(f"Duplicate migration order: {order}")
        seen_orders.add(order)
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(order=order, name=path.name, checksum=_checksum(sql), sql=sql)
        )
    return migrations


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL using SQLite's own completeness parser.

    Unlike the former line/semicolon splitter this handles trigger bodies,
    comments and semicolons inside quoted string literals.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for char in sql:
        buffer.append(char)
        if char == ";" and sqlite3.complete_statement("".join(buffer)):
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()
    tail = "".join(buffer).strip()
    if tail:
        if not sqlite3.complete_statement(f"{tail};"):
            raise MigrationError("Incomplete SQL statement at end of script")
        statements.append(tail)
    return statements


def _without_leading_comments(statement: str) -> str:
    stripped = statement.lstrip()
    while stripped.startswith("--"):
        _, _, stripped = stripped.partition("\n")
        stripped = stripped.lstrip()
    return stripped


def _is_pragma(statement: str) -> bool:
    return _without_leading_comments(statement).upper().startswith("PRAGMA ")


def _is_optional_vec_statement(migration: Migration, statement: str) -> bool:
    if migration.name not in _VEC_MIGRATIONS:
        return False
    normalized = " ".join(_without_leading_comments(statement).lower().split())
    return normalized.startswith("create virtual table") and " using vec0" in normalized


def _compatibility_error_allowed(statement: str, exc: BaseException) -> bool:
    """Allow only two reviewed legacy replay cases.

    No ``no such column`` suppression exists here.  ``already exists`` is only
    accepted for the one DDL form that historically omitted IF NOT EXISTS.
    """
    message = str(exc).lower()
    ddl = _without_leading_comments(statement)
    if "duplicate column name" in message and _ALTER_ADD_COLUMN_RE.match(ddl):
        return True
    return "already exists" in message and _CREATE_INDEX_RE.match(ddl) is not None


async def _execute_script(
    conn: aiosqlite.Connection,
    migration: Migration,
    *,
    sqlite_vec_loaded: bool,
) -> list[_CapabilityResult]:
    capabilities: list[_CapabilityResult] = []
    for statement in split_sql_statements(migration.sql):
        if _is_optional_vec_statement(migration, statement):
            capability_name = (
                "sqlite-vec/chat-message"
                if migration.order == 186
                else "sqlite-vec/screenshot"
            )
            capability_checksum = _checksum(statement)
            if not sqlite_vec_loaded:
                capabilities.append(
                    _CapabilityResult(
                        name=capability_name,
                        checksum=capability_checksum,
                        status="unavailable",
                    )
                )
                continue
            try:
                await conn.execute(statement)
            except Exception as exc:
                capabilities.append(
                    _CapabilityResult(
                        name=capability_name,
                        checksum=capability_checksum,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}"[:2000],
                    )
                )
                log.warning(
                    "migration.optional_capability_failed",
                    capability=capability_name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            else:
                capabilities.append(
                    _CapabilityResult(
                        name=capability_name,
                        checksum=capability_checksum,
                        status="applied",
                    )
                )
            continue
        try:
            await conn.execute(statement)
        except aiosqlite.OperationalError as exc:
            if not _compatibility_error_allowed(statement, exc):
                raise
            log.info(
                "migration.compatibility_statement_skipped",
                migration=migration.name,
                reason=str(exc),
            )
    return capabilities


async def _execute_schema(conn: aiosqlite.Connection, schema_sql: str) -> None:
    for statement in split_sql_statements(schema_sql):
        # Connection PRAGMAs are configured before BEGIN IMMEDIATE.  Repeating
        # journal_mode inside the migration transaction is invalid on a fresh DB.
        if not _is_pragma(statement):
            await conn.execute(statement)


async def _create_ledger_tables(conn: aiosqlite.Connection) -> None:
    for statement in split_sql_statements(_LEDGER_DDL):
        await conn.execute(statement)


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def _schema_manifest(
    conn: aiosqlite.Connection,
    *,
    object_names: set[str] | None = None,
) -> dict[str, _SchemaObjectShape]:
    """Describe every canonical object without depending on its row data."""
    cursor = await conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('schema_migration', 'schema_capability') "
        "ORDER BY name"
    )
    objects = await cursor.fetchall()
    manifest: dict[str, _SchemaObjectShape] = {}
    for raw_name, raw_kind in objects:
        name = str(raw_name)
        # A legacy DB can contain optional vec0 virtual/shadow tables even when
        # the extension is unavailable in the current release. Describing such
        # extra objects with PRAGMA table_xinfo would itself raise
        # ``no such module: vec0``. Baseline verification only needs canonical
        # objects from the extension-free reference manifest.
        if object_names is not None and name not in object_names:
            continue
        kind = str(raw_kind)
        # Column/FK/index/trigger PRAGMAs describe tables independently of
        # whether an old installation reached the same shape via ALTER TABLE
        # or a newer schema.sql. View bodies have no equivalent structural
        # PRAGMA, so their SQL definition is part of the fingerprint.
        definition_checksum = (
            await _object_definition_checksum(conn, kind=kind, name=name)
            if kind == "view"
            else ""
        )
        quoted = _quoted_identifier(name)
        column_cursor = await conn.execute(f"PRAGMA table_xinfo({quoted})")
        columns = tuple(
            (
                str(row[1]),
                str(row[2] or "").upper(),
                int(row[3]),
                str(row[4] or "").strip(),
                int(row[5]),
                int(row[6]),
            )
            for row in await column_cursor.fetchall()
        )

        foreign_keys: tuple[tuple[str, str, str, str, str, str], ...] = ()
        indexes: tuple[_IndexShape, ...] = ()
        if kind == "table":
            foreign_key_cursor = await conn.execute(
                f"PRAGMA foreign_key_list({quoted})"
            )
            foreign_keys = tuple(
                sorted(
                    (
                        str(row[2]),
                        str(row[3]),
                        str(row[4]),
                        str(row[5]),
                        str(row[6]),
                        str(row[7]),
                    )
                    for row in await foreign_key_cursor.fetchall()
                )
            )

            index_cursor = await conn.execute(f"PRAGMA index_list({quoted})")
            index_shapes: list[_IndexShape] = []
            for row in await index_cursor.fetchall():
                # Auto-index names are SQLite implementation details. Explicit
                # indexes ("c") are migration-owned and therefore canonical.
                if str(row[3]) != "c":
                    continue
                index_name = str(row[1])
                index_info = await conn.execute(
                    f"PRAGMA index_xinfo({_quoted_identifier(index_name)})"
                )
                index_columns = tuple(
                    (
                        str(info[2] or "<expression>"),
                        int(info[3]),
                        str(info[4] or ""),
                        int(info[5]),
                    )
                    for info in await index_info.fetchall()
                    if int(info[5]) == 1
                )
                index_shapes.append(
                    _IndexShape(
                        name=index_name,
                        unique=int(row[2]),
                        partial=int(row[4]),
                        columns=index_columns,
                        definition_checksum=await _object_definition_checksum(
                            conn,
                            kind="index",
                            name=index_name,
                        ),
                    )
                )
            indexes = tuple(sorted(index_shapes, key=lambda value: value.name))

        trigger_cursor = await conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = ? ORDER BY name",
            (name,),
        )
        triggers = tuple(
            (str(row[0]), _checksum(str(row[1] or "")))
            for row in await trigger_cursor.fetchall()
        )
        manifest[name] = _SchemaObjectShape(
            kind=kind,
            definition_checksum=definition_checksum,
            columns=columns,
            foreign_keys=foreign_keys,
            indexes=indexes,
            triggers=triggers,
        )
    return manifest


async def _object_definition_checksum(
    conn: aiosqlite.Connection,
    *,
    kind: str,
    name: str,
) -> str:
    cursor = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
        (kind, name),
    )
    row = await cursor.fetchone()
    return _checksum(str(row[0] or "") if row is not None else "")


async def _reference_schema_manifest(
    schema_sql: str,
    migrations: list[Migration],
) -> dict[str, _SchemaObjectShape]:
    """Build the release's complete structural fingerprint in disposable RAM."""
    async with aiosqlite.connect(":memory:", isolation_level=None) as reference:
        await reference.execute("PRAGMA foreign_keys = ON")
        await reference.execute("BEGIN IMMEDIATE")
        try:
            await _execute_schema(reference, schema_sql)
            for migration in migrations:
                await _execute_script(
                    reference,
                    migration,
                    sqlite_vec_loaded=False,
                )
            return await _schema_manifest(reference)
        finally:
            await reference.execute("ROLLBACK")


async def _legacy_schema_is_current(
    conn: aiosqlite.Connection,
    expected: dict[str, _SchemaObjectShape],
) -> tuple[bool, list[str]]:
    """Prove every canonical schema object before recording a full baseline."""
    actual = await _schema_manifest(conn, object_names=set(expected))
    differences: list[str] = []
    for name, expected_shape in expected.items():
        actual_shape = actual.get(name)
        if actual_shape is None:
            differences.append(name)
            continue
        if actual_shape.kind != expected_shape.kind:
            differences.append(f"{name}.kind")
        if actual_shape.definition_checksum != expected_shape.definition_checksum:
            differences.append(f"{name}.definition")
        if actual_shape.columns != expected_shape.columns:
            differences.append(f"{name}.columns")
        if actual_shape.foreign_keys != expected_shape.foreign_keys:
            differences.append(f"{name}.foreign_keys")
        if actual_shape.indexes != expected_shape.indexes:
            differences.append(f"{name}.indexes")
        if actual_shape.triggers != expected_shape.triggers:
            differences.append(f"{name}.triggers")
    return not differences, differences


async def _select_legacy_baseline(
    conn: aiosqlite.Connection,
    *,
    schema_sql: str,
    migrations: list[Migration],
) -> tuple[list[Migration], list[str]]:
    """Prove either release head or the reviewed pre-ledger boundary."""

    candidates = [migrations]
    boundary = [
        migration
        for migration in migrations
        if migration.order <= _LEGACY_LEDGER_BOUNDARY_ORDER
    ]
    if len(boundary) != len(migrations):
        candidates.append(boundary)

    all_differences: list[str] = []
    for candidate in candidates:
        expected_schema = await _reference_schema_manifest(
            schema_sql,
            candidate,
        )
        compatible, differences = await _legacy_schema_is_current(
            conn,
            expected_schema,
        )
        if compatible:
            return candidate, []
        label = candidate[-1].name if candidate else "schema.sql"
        all_differences.extend(f"{label}:{item}" for item in differences)
    return [], all_differences


async def _ledger_exists(conn: aiosqlite.Connection) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'schema_migration'"
    )
    return await cursor.fetchone() is not None


async def _ledger_has_rows(conn: aiosqlite.Connection) -> bool:
    cursor = await conn.execute("SELECT 1 FROM schema_migration LIMIT 1")
    return await cursor.fetchone() is not None


async def _has_application_schema(conn: aiosqlite.Connection) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('schema_migration', 'schema_capability') LIMIT 1"
    )
    return await cursor.fetchone() is not None


async def _verify_ledger(
    conn: aiosqlite.Connection,
    migrations: list[Migration],
) -> set[str]:
    cursor = await conn.execute(
        "SELECT migration_order, name, checksum, status, error "
        "FROM schema_migration ORDER BY migration_order"
    )
    rows = list(await cursor.fetchall())
    failed = [row for row in rows if row[3] == "failed"]
    if failed:
        row = failed[0]
        raise MigrationFailedState(
            f"Migration {row[1]} previously failed: {row[4] or 'unknown error'}"
        )

    manifest = {migration.name: migration for migration in migrations}
    applied_names = [str(row[1]) for row in rows]
    expected_prefix = [migration.name for migration in migrations[: len(rows)]]
    if applied_names != expected_prefix:
        raise MigrationChecksumError(
            "Migration history is not an append-only manifest prefix: "
            f"ledger={applied_names!r}, expected={expected_prefix!r}"
        )
    applied: set[str] = set()
    for row in rows:
        order, name, checksum = int(row[0]), str(row[1]), str(row[2])
        migration = manifest.get(name)
        if migration is None:
            raise MigrationChecksumError(
                f"Applied migration is missing from this release: {name}"
            )
        if migration.order != order:
            raise MigrationChecksumError(
                f"Migration order changed for {name}: ledger={order}, file={migration.order}"
            )
        if migration.checksum != checksum:
            raise MigrationChecksumError(f"Checksum mismatch for applied migration {name}")
        applied.add(name)
    return applied


async def _record_capabilities(
    conn: aiosqlite.Connection,
    capabilities: list[_CapabilityResult],
) -> None:
    for capability in capabilities:
        await conn.execute(
            """
            INSERT INTO schema_capability
                (name, checksum, status, checked_at, applied_at, error)
            VALUES (?, ?, ?, datetime('now'),
                    CASE WHEN ? = 'applied' THEN datetime('now') END, ?)
            ON CONFLICT(name) DO UPDATE SET
                checksum = excluded.checksum,
                status = excluded.status,
                checked_at = excluded.checked_at,
                applied_at = CASE
                    WHEN excluded.status = 'applied'
                    THEN COALESCE(schema_capability.applied_at, excluded.applied_at)
                    ELSE schema_capability.applied_at
                END,
                error = excluded.error
            """,
            (
                capability.name,
                capability.checksum,
                capability.status,
                capability.status,
                capability.error,
            ),
        )


async def _ensure_vec_capabilities(
    conn: aiosqlite.Connection,
    migrations: list[Migration],
    *,
    sqlite_vec_loaded: bool,
) -> None:
    """Install optional vec0 tables when the extension becomes available later."""
    for migration in migrations:
        if migration.name not in _VEC_MIGRATIONS:
            continue
        for statement in split_sql_statements(migration.sql):
            if not _is_optional_vec_statement(migration, statement):
                continue
            name = (
                "sqlite-vec/chat-message"
                if migration.order == 186
                else "sqlite-vec/screenshot"
            )
            checksum = _checksum(statement)
            cursor = await conn.execute(
                "SELECT checksum, status FROM schema_capability WHERE name = ?",
                (name,),
            )
            row = await cursor.fetchone()
            if row is not None and str(row[0]) != checksum:
                raise MigrationChecksumError(f"Checksum mismatch for capability {name}")
            if row is not None:
                already_current = (
                    (sqlite_vec_loaded and row[1] == "applied")
                    or (not sqlite_vec_loaded and row[1] == "unavailable")
                )
                if already_current:
                    continue
            if sqlite_vec_loaded and (row is None or row[1] != "applied"):
                try:
                    await conn.execute(statement)
                except Exception as exc:
                    await _record_capabilities(
                        conn,
                        [
                            _CapabilityResult(
                                name=name,
                                checksum=checksum,
                                status="failed",
                                error=f"{type(exc).__name__}: {exc}"[:2000],
                            )
                        ],
                    )
                    log.warning(
                        "migration.optional_capability_failed",
                        capability=name,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    continue
            await _record_capabilities(
                conn,
                [
                    _CapabilityResult(
                        name=name,
                        checksum=checksum,
                        status="applied" if sqlite_vec_loaded else "unavailable",
                    )
                ],
            )


async def _insert_baseline(
    conn: aiosqlite.Connection,
    migrations: list[Migration],
) -> None:
    for migration in migrations:
        await conn.execute(
            """
            INSERT INTO schema_migration
                (migration_order, name, checksum, status, started_at,
                 finished_at, applied_at, duration_ms, error, is_baseline)
            VALUES (?, ?, ?, 'applied', datetime('now'), datetime('now'),
                    datetime('now'), 0, NULL, 1)
            """,
            (migration.order, migration.name, migration.checksum),
        )


async def _apply_pending(
    conn: aiosqlite.Connection,
    migrations: list[Migration],
    applied: set[str],
    *,
    sqlite_vec_loaded: bool,
) -> tuple[int, int]:
    applied_count = 0
    total_duration_ms = 0
    for migration in migrations:
        if migration.name in applied:
            continue
        started = time.perf_counter()
        try:
            capabilities = await _execute_script(
                conn,
                migration,
                sqlite_vec_loaded=sqlite_vec_loaded,
            )
        except Exception as exc:
            raise _MigrationBodyError(migration, exc) from exc
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        await conn.execute(
            """
            INSERT INTO schema_migration
                (migration_order, name, checksum, status, started_at,
                 finished_at, applied_at, duration_ms, error, is_baseline)
            VALUES (?, ?, ?, 'applied', datetime('now'), datetime('now'),
                    datetime('now'), ?, NULL, 0)
            """,
            (migration.order, migration.name, migration.checksum, duration_ms),
        )
        await _record_capabilities(
            conn,
            capabilities,
        )
        applied_count += 1
        total_duration_ms += duration_ms
        log.debug(
            "migration.applied",
            migration=migration.name,
            duration_ms=duration_ms,
        )
    return applied_count, total_duration_ms


async def _persist_failure(
    conn: aiosqlite.Connection,
    migration: Migration | None,
    exc: BaseException,
) -> None:
    """Persist a visible failure after the migration transaction rolls back."""
    name = migration.name if migration is not None else "__legacy_baseline__.sql"
    order = migration.order if migration is not None else 0
    checksum = migration.checksum if migration is not None else _checksum(name)
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await _create_ledger_tables(conn)
        await conn.execute(
            """
            INSERT INTO schema_migration
                (migration_order, name, checksum, status, started_at,
                 finished_at, applied_at, duration_ms, error, is_baseline)
            VALUES (?, ?, ?, 'failed', datetime('now'), datetime('now'),
                    NULL, NULL, ?, 0)
            ON CONFLICT(name) DO UPDATE SET
                checksum=excluded.checksum, status='failed',
                finished_at=excluded.finished_at, applied_at=NULL,
                duration_ms=NULL, error=excluded.error
            """,
            (order, name, checksum, str(exc)[:2000]),
        )
        await conn.execute("COMMIT")
    except BaseException:
        await conn.execute("ROLLBACK")
        raise


async def migrate(
    conn: aiosqlite.Connection,
    *,
    schema_sql: str,
    migrations_dir: Path,
    sqlite_vec_loaded: bool,
) -> None:
    """Apply or verify all migrations under one concurrent-startup-safe lock."""
    migrations = discover_migrations(migrations_dir)
    await conn.execute("BEGIN IMMEDIATE")
    try:
        had_ledger = await _ledger_exists(conn)
        has_application_schema = await _has_application_schema(conn)
        await _create_ledger_tables(conn)
        ledger_has_rows = had_ledger and await _ledger_has_rows(conn)

        # A repaired legacy database may retain empty ledger tables after the
        # operator removes the reviewed synthetic failure row.  Treat that
        # state exactly like the first legacy bootstrap: prove schema head and
        # baseline it.  Replaying all historical migrations here can rebuild
        # FTS tables and overwrite operator-managed seed configuration.
        if has_application_schema and not ledger_has_rows:
            baseline, differences = await _select_legacy_baseline(
                conn,
                schema_sql=schema_sql,
                migrations=migrations,
            )
            if not baseline:
                raise LegacySchemaError(
                    "Legacy database is not provably at schema head or the "
                    "reviewed pre-ledger boundary; differences: "
                    + ", ".join(differences)
                )
            await _insert_baseline(conn, baseline)
            applied = {migration.name for migration in baseline}
            log.info("migration.legacy_baseline_created", count=len(applied))
        else:
            if not has_application_schema:
                await _execute_schema(conn, schema_sql)
            applied = await _verify_ledger(conn, migrations)

        applied_count, total_duration_ms = await _apply_pending(
            conn,
            migrations,
            applied,
            sqlite_vec_loaded=sqlite_vec_loaded,
        )
        await _ensure_vec_capabilities(
            conn,
            migrations,
            sqlite_vec_loaded=sqlite_vec_loaded,
        )
        await conn.execute("COMMIT")
        if applied_count:
            log.info(
                "migration.batch_applied",
                count=applied_count,
                duration_ms=total_duration_ms,
            )
    except BaseException as exc:
        with suppress(aiosqlite.OperationalError):
            await conn.execute("ROLLBACK")
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, (MigrationChecksumError, MigrationFailedState)):
            raise
        failed_migration = exc.migration if isinstance(exc, _MigrationBodyError) else None
        failure_cause = exc.cause if isinstance(exc, _MigrationBodyError) else exc
        await _persist_failure(conn, failed_migration, failure_cause)
        if isinstance(exc, _MigrationBodyError):
            raise failure_cause from exc
        raise
