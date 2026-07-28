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
) -> list[tuple[str, str]]:
    capabilities: list[tuple[str, str]] = []
    for statement in split_sql_statements(migration.sql):
        if _is_optional_vec_statement(migration, statement):
            capability_name = (
                "sqlite-vec/chat-message"
                if migration.order == 186
                else "sqlite-vec/screenshot"
            )
            capability_checksum = _checksum(statement)
            capabilities.append((capability_name, capability_checksum))
            if not sqlite_vec_loaded:
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


async def _table_names(conn: aiosqlite.Connection) -> set[str]:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    )
    return {str(row[0]) for row in await cursor.fetchall()}


async def _column_names(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f'PRAGMA table_info("{table}")')
    return {str(row[1]) for row in await cursor.fetchall()}


async def _legacy_schema_is_current(conn: aiosqlite.Connection) -> tuple[bool, list[str]]:
    """Check the reviewed schema-head sentinels used for one-time baselining."""
    tables = await _table_names(conn)
    required_tables = {
        "screenshots",
        "chat_message",
        "chat_message_fts",
        "audio_segment",
        "audio_segment_fts",
        "mcp_server",
        "kg_entity",
        "kg_edge",
        "tool_artifact",
        "system_log",
        "llm_job",
        "llm_job_chunk",
    }
    missing = sorted(required_tables - tables)
    if "mcp_server" in tables and "timeout_ms" not in await _column_names(
        conn, "mcp_server"
    ):
        missing.append("mcp_server.timeout_ms")

    if "kg_edge" in tables:
        cursor = await conn.execute('PRAGMA foreign_key_list("kg_edge")')
        fk_targets = {str(row[2]) for row in await cursor.fetchall()}
        if "kg_entity" not in fk_targets:
            missing.append("kg_edge foreign keys -> kg_entity")

    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
        "('idx_llm_job_status', 'idx_llm_job_chunk', 'idx_tool_artifact_exec', "
        "'idx_system_log_ts')"
    )
    indexes = {str(row[0]) for row in await cursor.fetchall()}
    for index in (
        "idx_llm_job_status",
        "idx_llm_job_chunk",
        "idx_tool_artifact_exec",
        "idx_system_log_ts",
    ):
        if index not in indexes:
            missing.append(index)
    return not missing, missing


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
    rows = await cursor.fetchall()
    failed = [row for row in rows if row[3] == "failed"]
    if failed:
        row = failed[0]
        raise MigrationFailedState(
            f"Migration {row[1]} previously failed: {row[4] or 'unknown error'}"
        )

    manifest = {migration.name: migration for migration in migrations}
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
    capabilities: list[tuple[str, str]],
    *,
    sqlite_vec_loaded: bool,
) -> None:
    status = "applied" if sqlite_vec_loaded else "unavailable"
    for name, checksum in capabilities:
        await conn.execute(
            """
            INSERT INTO schema_capability
                (name, checksum, status, checked_at, applied_at, error)
            VALUES (?, ?, ?, datetime('now'),
                    CASE WHEN ? = 'applied' THEN datetime('now') END, NULL)
            ON CONFLICT(name) DO UPDATE SET
                checksum = excluded.checksum,
                status = excluded.status,
                checked_at = excluded.checked_at,
                applied_at = CASE
                    WHEN excluded.status = 'applied'
                    THEN COALESCE(schema_capability.applied_at, excluded.applied_at)
                    ELSE schema_capability.applied_at
                END,
                error = NULL
            """,
            (name, checksum, status, status),
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
                    await conn.execute(
                        """
                        INSERT INTO schema_capability
                            (name, checksum, status, checked_at, error)
                        VALUES (?, ?, 'failed', datetime('now'), ?)
                        ON CONFLICT(name) DO UPDATE SET
                            checksum=excluded.checksum, status='failed',
                            checked_at=excluded.checked_at, error=excluded.error
                        """,
                        (name, checksum, str(exc)[:2000]),
                    )
                    raise
            await _record_capabilities(
                conn,
                [(name, checksum)],
                sqlite_vec_loaded=sqlite_vec_loaded,
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
        except BaseException as exc:
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
            sqlite_vec_loaded=sqlite_vec_loaded,
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
            compatible, missing = await _legacy_schema_is_current(conn)
            if not compatible:
                raise LegacySchemaError(
                    "Legacy database is not provably at schema head; missing: "
                    + ", ".join(missing)
                )
            await _insert_baseline(conn, migrations)
            applied = {migration.name for migration in migrations}
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
        if isinstance(exc, (MigrationChecksumError, MigrationFailedState)):
            raise
        failed_migration = exc.migration if isinstance(exc, _MigrationBodyError) else None
        failure_cause = exc.cause if isinstance(exc, _MigrationBodyError) else exc
        await _persist_failure(conn, failed_migration, failure_cause)
        if isinstance(exc, _MigrationBodyError):
            raise failure_cause from exc
        raise
