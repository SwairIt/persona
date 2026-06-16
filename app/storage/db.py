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
    # v1.66 — индексы/таблицы из v1.40+ миграций иногда создаются без
    # IF NOT EXISTS; повторный прогон на уже-migrated DB падал на
    # 'index ... already exists'. Раньше это не было заметно потому что
    # uvicorn запускался один раз и init_database выполнялся один раз
    # за сессию.
    "already exists",
    # v186 — миграция vec0-таблиц (sqlite-vec) ОПЦИОНАЛЬНА. Без расширения
    # ``CREATE VIRTUAL TABLE ... USING vec0`` падает с этой ошибкой — трактуем
    # как no-op: построчный replay пропустит только vec0-строки.
    "no such module",
)

log = get_logger("persona.storage.db")

# ── sqlite-vec (векторный KNN) — ОПЦИОНАЛЬНО, с тихим fallback ────────────────
# Расширения SQLite — per-connection. Пробу пакета делаем один раз (кэш), саму
# загрузку — на каждом соединении. Если пакета/бинарника нет — векторный путь
# тихо отключается, FTS5/LIKE recall продолжают работать как раньше.
_VEC_PATH: str | None = None
_VEC_PROBED: bool = False
_VEC_USABLE: bool | None = None


def sqlite_vec_available() -> bool:
    """True, если sqlite-vec хоть раз успешно загрузилось (дешёвый гейт)."""
    return _VEC_USABLE is True


def _vec_loadable_path() -> str | None:
    global _VEC_PATH, _VEC_PROBED
    if _VEC_PROBED:
        return _VEC_PATH
    _VEC_PROBED = True
    try:
        import sqlite_vec  # noqa: PLC0415 — опциональная зависимость

        _VEC_PATH = sqlite_vec.loadable_path()
    except Exception as exc:  # noqa: BLE001 — пакет не установлен / нет бинарника
        log.info("sqlite_vec.unavailable", reason=str(exc))
        _VEC_PATH = None
    return _VEC_PATH


async def _load_sqlite_vec(conn: aiosqlite.Connection) -> bool:
    """Загрузить sqlite-vec на соединение. Любая ошибка → False, соединение цело."""
    global _VEC_USABLE
    path = _vec_loadable_path()
    if not path:
        _VEC_USABLE = False
        return False
    try:
        await conn.enable_load_extension(True)
        await conn.load_extension(path)
        await conn.enable_load_extension(False)
        _VEC_USABLE = True
        return True
    except Exception as exc:  # noqa: BLE001 — сборка без load_extension / ABI
        if _VEC_USABLE is None:
            log.info("sqlite_vec.load_failed", reason=str(exc))
        _VEC_USABLE = False
        try:
            await conn.enable_load_extension(False)
        except Exception:  # noqa: BLE001, S110
            pass
        return False


async def init_database(db_path: Path | None = None) -> None:
    """Create the SQLite database, apply schema and all migrations. Idempotent."""
    target = db_path or get_settings().db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with aiosqlite.connect(target) as conn:
        # Грузим sqlite-vec ДО миграций: миграция 186 создаёт vec0-таблицы;
        # без расширения CREATE VIRTUAL TABLE USING vec0 ловится как
        # идемпотентная 'no such module' и пропускается.
        await _load_sqlite_vec(conn)
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


async def _configure_connection(conn: aiosqlite.Connection) -> None:
    """Применить прагмы + sqlite-vec + row_factory к свежему соединению.

    Общий код для get_connection и write_transaction, чтобы настройки не
    разъезжались между чтением и записью.
    """
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA synchronous = NORMAL")
    await conn.execute("PRAGMA foreign_keys = ON")
    # T19 fix (2026-06-07) — without busy_timeout the second writer
    # gets ``SQLITE_BUSY`` immediately and dies. With 40 background
    # workers all heartbeating every 3 sec, plus a slow Ollama call
    # holding a write transaction, lock contention spikes — the user
    # reports the site freezes. 5000ms gives writers time to queue
    # politely instead of throwing.
    await conn.execute("PRAGMA busy_timeout = 5000")
    # Read-speed pragmas (cheap, per-connection) — help FTS5 bm25 sorts,
    # vector KNN temp sorts, and large scans. mmap 256MB, 64MB page cache,
    # temp tables/indexes in RAM. Best-effort: a build that rejects a
    # pragma must never break the connection.
    try:
        await conn.execute("PRAGMA mmap_size = 268435456")
        await conn.execute("PRAGMA cache_size = -65536")
        await conn.execute("PRAGMA temp_store = MEMORY")
    except Exception:  # noqa: BLE001, S110
        pass
    # sqlite-vec per-connection (best-effort). Если расширения нет —
    # _VEC_USABLE станет False и больше не пробуем; векторный путь тихо
    # отключён, FTS/LIKE recall работают.
    if _VEC_USABLE is not False:
        await _load_sqlite_vec(conn)
    conn.row_factory = aiosqlite.Row


@asynccontextmanager
async def get_connection(
    db_path: Path | None = None,
) -> AsyncIterator[aiosqlite.Connection]:
    """Yield an async SQLite connection with sane pragmas."""
    target = db_path or get_settings().db_path
    async with aiosqlite.connect(target) as conn:
        await _configure_connection(conn)
        yield conn


@asynccontextmanager
async def write_transaction(
    db_path: Path | None = None,
) -> AsyncIterator[aiosqlite.Connection]:
    """Соединение в ЯВНОЙ ``BEGIN IMMEDIATE``-транзакции для записи.

    ``BEGIN IMMEDIATE`` сразу берёт RESERVED write-lock — это устраняет класс
    дедлоков при апгрейде read→write под конкуренцией (см. T19: 40 воркеров +
    медленный Ollama). Коммитит при успехе, откатывает при исключении.

    ВАЖНО: держать транзакцию КОРОТКОЙ — НИКАКИХ LLM/сетевых вызовов внутри,
    иначе заблокируешь всех писателей на время вызова. Делать только сами
    INSERT/UPDATE/DELETE, а тяжёлую работу (эмбеддинги, LLM) — ДО/ПОСЛЕ блока.
    """
    target = db_path or get_settings().db_path
    # isolation_level=None (autocommit) задаём при connect — в рабочем потоке
    # aiosqlite; ставить через свойство нельзя (cross-thread ProgrammingError).
    # В autocommit драйвер не делает неявных BEGIN, поэтому выдаём BEGIN IMMEDIATE сами.
    async with aiosqlite.connect(target, isolation_level=None) as conn:
        await _configure_connection(conn)
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            await conn.execute("COMMIT")
        except BaseException:
            try:
                await conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001, S110
                pass
            raise
