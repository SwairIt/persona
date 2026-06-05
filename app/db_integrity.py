"""DB integrity quick-check + ANALYZE helpers (v1.51).

The operator runs Persona on a laptop where the SQLite file gets all
the usual abuse (cloud syncing, sleep/wake, the occasional ``kill -9``
of the daemon). SQLite is robust, but "robust" is statistical — once a
year a torn page or a free-list inconsistency does sneak in, and the
sooner we catch it the smaller the blast radius.

This module is the one-shot core that the daily worker
(:mod:`app.workers.db_integrity_worker`) and the "Run Now" buttons on
:mod:`app.web.routes.db_integrity` both call. It runs one of three
PRAGMAs against the live connection, times the round-trip, records a
row in ``db_integrity_run`` (migration 139), and returns a small dict
the caller can render.

Three callable kinds
--------------------
* :func:`run_quick_check` — ``PRAGMA quick_check``. Cheap (single
  pass over the b-tree pages, no cross-page verification); the daily
  worker fires it nightly.
* :func:`run_full_check`  — ``PRAGMA integrity_check``. Slow but
  thorough (rewalks every page + index). Only fired by the operator
  pressing the "Run Full Check" button on the admin page.
* :func:`run_analyze`     — ``PRAGMA optimize`` + ``ANALYZE``. Not an
  integrity check per se — it refreshes the query-planner statistics
  the rest of the codebase depends on. Bundled here because the
  scheduler runs it on the same nightly tick as ``quick_check`` so
  the operator only has one knob to think about.

Status semantics
----------------
``run_quick_check`` and ``run_full_check`` both return ``status`` in
``{"ok", "warning", "error"}``:

* ``ok``      — PRAGMA returned literally ``ok``.
* ``warning`` — PRAGMA returned a non-``ok`` row (e.g. ``*** in
  database main *** Page 17: btreeInitPage: error reading page``).
  The text lives in ``result``; the operator should investigate.
* ``error``   — the PRAGMA itself raised (typically ``aiosqlite.Error``
  or ``OSError``). The exception message lives in ``result``.

``run_analyze`` only emits ``ok`` / ``error`` — ANALYZE has no "the
data is fine but the index is wonky" half-way state.

All SQL is parametrised; PRAGMAs themselves have no user input.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Iterable

log = get_logger("persona.db_integrity")


class IntegrityResult(TypedDict):
    """Return shape of every public coroutine in this module."""

    result: str
    duration_ms: int
    db_size_bytes: int
    status: str


_CHECK_KIND_QUICK: str = "quick"
_CHECK_KIND_FULL: str = "full"
_CHECK_KIND_ANALYZE: str = "analyze"

#: Recent-runs default page size for :func:`list_recent_runs`. The
#: admin page lists the most recent 20 runs by default — enough to
#: scan two days of activity if the operator clicks "Run Now" a lot.
_DEFAULT_HISTORY_LIMIT: int = 20


async def run_quick_check() -> IntegrityResult:
    """Run ``PRAGMA quick_check``, record a row, return the result.

    Cheap enough to run nightly. The daily worker
    (:mod:`app.workers.db_integrity_worker`) fires this + ANALYZE on
    every tick. The operator can also trigger it from the admin page
    via ``POST /api/db-integrity/quick-check``.
    """
    return await _run_pragma_check(_CHECK_KIND_QUICK, "PRAGMA quick_check")


async def run_full_check() -> IntegrityResult:
    """Run ``PRAGMA integrity_check``, record a row, return the result.

    Slower (rewalks every page + index); the operator opts into this
    via the "Run Full Check" button on the admin page. Never fired by
    the scheduler — too long for a single tick.
    """
    return await _run_pragma_check(_CHECK_KIND_FULL, "PRAGMA integrity_check")


async def run_analyze() -> IntegrityResult:
    """Refresh query-planner stats via ``PRAGMA optimize`` + ``ANALYZE``.

    Not an integrity check — it makes the existing data faster to
    query, not safer. Bundled in this module because the scheduler
    runs it on the same nightly tick as :func:`run_quick_check`.

    Returns the same shape as the integrity coroutines; ``result`` is
    ``ok`` on success or the exception message on failure.
    """
    started_at = time.monotonic()
    status = "ok"
    result_text = "ok"
    try:
        async with get_connection() as conn:
            # PRAGMA optimize runs ANALYZE on tables that need it; the
            # explicit ANALYZE after is belt-and-braces on tables that
            # optimize chose to skip (e.g. very small tables). Both are
            # cheap; running them in sequence keeps the stats fresh
            # without us having to reason about heuristics.
            await conn.execute("PRAGMA optimize")
            await conn.execute("ANALYZE")
            await conn.commit()
            db_size = await _read_db_size_bytes(conn)
    except (aiosqlite.Error, OSError) as exc:
        status = "error"
        result_text = f"ANALYZE raised: {exc}"
        db_size = 0
        log.exception("db_integrity.analyze.failed", error=str(exc))

    duration_ms = int((time.monotonic() - started_at) * 1000)
    await _record_run(
        check_kind=_CHECK_KIND_ANALYZE,
        result=result_text,
        duration_ms=duration_ms,
        db_size_bytes=db_size,
    )
    log.info(
        "db_integrity.analyze.done",
        status=status,
        duration_ms=duration_ms,
        db_size_bytes=db_size,
    )
    return IntegrityResult(
        result=result_text,
        duration_ms=duration_ms,
        db_size_bytes=db_size,
        status=status,
    )


async def list_recent_runs(limit: int = _DEFAULT_HISTORY_LIMIT) -> list[dict[str, object]]:
    """Return the N most recent ``db_integrity_run`` rows newest-first.

    Used by the admin page (history table) and the JSON history
    endpoint. ``limit`` is clamped to ``[1, 500]`` to keep the response
    bounded regardless of caller input.
    """
    bounded = max(1, min(int(limit), 500))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, ran_at, check_kind, result, duration_ms, db_size_bytes "
            "FROM db_integrity_run ORDER BY id DESC LIMIT ?",
            (bounded,),
        )
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def _run_pragma_check(check_kind: str, pragma_sql: str) -> IntegrityResult:
    """Shared implementation for ``quick_check`` + ``integrity_check``.

    Times the PRAGMA round-trip, classifies the verdict, and records a
    ``db_integrity_run`` row regardless of outcome — even the failure
    cases are useful history (the operator wants to see the dip).
    """
    started_at = time.monotonic()
    status: str
    result_text: str
    db_size: int

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(pragma_sql)
            rows = await cursor.fetchall()
            db_size = await _read_db_size_bytes(conn)
    except (aiosqlite.Error, OSError) as exc:
        status = "error"
        result_text = f"{pragma_sql} raised: {exc}"
        db_size = 0
        log.exception(
            "db_integrity.check.failed",
            check_kind=check_kind,
            error=str(exc),
        )
    else:
        result_text = _stringify_pragma_rows(rows)
        status = "ok" if result_text.strip().lower() == "ok" else "warning"

    duration_ms = int((time.monotonic() - started_at) * 1000)
    await _record_run(
        check_kind=check_kind,
        result=result_text,
        duration_ms=duration_ms,
        db_size_bytes=db_size,
    )
    log.info(
        "db_integrity.check.done",
        check_kind=check_kind,
        status=status,
        duration_ms=duration_ms,
        db_size_bytes=db_size,
    )
    return IntegrityResult(
        result=result_text,
        duration_ms=duration_ms,
        db_size_bytes=db_size,
        status=status,
    )


async def _read_db_size_bytes(conn: aiosqlite.Connection) -> int:
    """Return ``page_count * page_size`` for the open connection.

    The two PRAGMAs are independent; we read them sequentially and
    multiply. On any failure we return 0 — the size column has
    ``DEFAULT 0`` and downstream UIs already tolerate it.
    """
    try:
        cursor = await conn.execute("PRAGMA page_count")
        row = await cursor.fetchone()
        page_count = int(row[0]) if row else 0
        cursor = await conn.execute("PRAGMA page_size")
        row = await cursor.fetchone()
        page_size = int(row[0]) if row else 0
    except (aiosqlite.Error, OSError) as exc:
        log.warning("db_integrity.size.failed", error=str(exc))
        return 0
    return page_count * page_size


def _stringify_pragma_rows(rows: Iterable[aiosqlite.Row]) -> str:
    """Flatten the rows a ``PRAGMA *_check`` returns into a single string.

    A healthy DB returns exactly one row whose first column is ``ok``.
    A damaged DB returns one row per problem, each a free-form text
    diagnostic. We join with newlines so the UI can render the verbatim
    text in a ``<pre>`` block.
    """
    parts: list[str] = []
    for row in rows:
        first = row[0]
        parts.append(str(first) if first is not None else "")
    return "\n".join(parts)


async def _record_run(
    *,
    check_kind: str,
    result: str,
    duration_ms: int,
    db_size_bytes: int,
) -> None:
    """Insert one row into ``db_integrity_run``.

    Failures here are logged but never re-raised — losing one
    bookkeeping row is preferable to surfacing a follow-on error in a
    code path that was just verifying the DB is healthy.
    """
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO db_integrity_run "
                "(check_kind, result, duration_ms, db_size_bytes) "
                "VALUES (?, ?, ?, ?)",
                (check_kind, result, int(duration_ms), int(db_size_bytes)),
            )
            await conn.commit()
    except (aiosqlite.Error, OSError) as exc:
        log.warning(
            "db_integrity.record.failed",
            check_kind=check_kind,
            error=str(exc),
        )


def _row_to_dict(row: aiosqlite.Row) -> dict[str, object]:
    """Convert one aiosqlite Row of ``db_integrity_run`` to a JSON-safe dict."""
    return {
        "id": int(row["id"]),
        "ran_at": str(row["ran_at"]),
        "check_kind": str(row["check_kind"]),
        "result": str(row["result"]),
        "duration_ms": int(row["duration_ms"]),
        "db_size_bytes": int(row["db_size_bytes"]),
    }


__all__ = [
    "IntegrityResult",
    "list_recent_runs",
    "run_analyze",
    "run_full_check",
    "run_quick_check",
]
