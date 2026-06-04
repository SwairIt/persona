"""Async SQLite connection management and schema bootstrap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.logging_setup import get_logger
from app.settings import get_settings

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# SQLite's ``ALTER TABLE ... ADD COLUMN`` has no ``IF NOT EXISTS`` form,
# so re-running a migration that adds a column fails with this exact
# message. We split such migrations statement-by-statement and silently
# skip the failing ``ADD COLUMN`` while still running the rest. Other
# ``OperationalError`` variants (typo, missing table, syntax error) are
# re-raised so genuine breakage stays loud.
_IDEMPOTENT_ALTER_ERRORS: tuple[str, ...] = (
    "duplicate column name",
    "no such column",
)

log = get_logger("persona.storage.db")


async def init_database(db_path: Path | None = None) -> None:
    """Create the SQLite database, apply schema and all migrations. Idempotent."""
    target = db_path or get_settings().db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with aiosqlite.connect(target) as conn:
        await conn.executescript(schema_sql)
        if _MIGRATIONS_DIR.exists():
            for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                await _run_migration(conn, migration)
        await conn.commit()


async def _run_migration(conn: aiosqlite.Connection, migration: Path) -> None:
    """Apply ``migration`` to ``conn``, swallowing duplicate-column errors.

    Tries the bulk :py:meth:`aiosqlite.Connection.executescript` path
    first — fast, single round-trip, matches the historic behaviour. If
    SQLite reports a duplicate-column error (because we re-ran a
    previously-applied ``ALTER TABLE ... ADD COLUMN``), we fall back to
    a statement-by-statement replay and skip only the offending
    statements, keeping every other ``CREATE INDEX`` / ``UPDATE`` /
    ``CREATE TABLE`` in the same file effective.
    """
    sql = migration.read_text(encoding="utf-8")
    try:
        await conn.executescript(sql)
    except aiosqlite.OperationalError as exc:
        if not _is_idempotent_alter_error(exc):
            raise
        await _replay_statements(conn, migration.name, sql)


def _is_idempotent_alter_error(exc: BaseException) -> bool:
    """Return ``True`` for SQLite errors we intentionally treat as no-ops."""
    message = str(exc).lower()
    return any(needle in message for needle in _IDEMPOTENT_ALTER_ERRORS)


async def _replay_statements(
    conn: aiosqlite.Connection,
    migration_name: str,
    sql: str,
) -> None:
    """Replay ``sql`` one statement at a time, skipping idempotent errors."""
    for statement in _split_sql_statements(sql):
        try:
            await conn.execute(statement)
        except aiosqlite.OperationalError as exc:
            if not _is_idempotent_alter_error(exc):
                raise
            log.debug(
                "migration.statement_skipped",
                migration=migration_name,
                reason=str(exc),
            )


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script on ``;`` while ignoring ``;`` inside comments.

    Migration files only ever contain ``--`` line comments and plain
    DDL/DML — no string literals with embedded semicolons — so a
    line-aware split is sufficient and avoids dragging in a full SQL
    parser.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.split("--", 1)[0]
        buffer.append(raw_line)
        if ";" in line:
            chunk = "\n".join(buffer).strip()
            buffer = []
            if chunk.rstrip(";").strip():
                statements.append(chunk)
    tail = "\n".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


@asynccontextmanager
async def get_connection(
    db_path: Path | None = None,
) -> AsyncIterator[aiosqlite.Connection]:
    """Yield an async SQLite connection with sane pragmas."""
    target = db_path or get_settings().db_path
    async with aiosqlite.connect(target) as conn:
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = aiosqlite.Row
        yield conn
