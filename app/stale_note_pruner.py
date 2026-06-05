"""Stale-note pruner: auto-soft-delete empty notes older than N days (v1.49).

The inbox notes table (migration 039) accumulates rows whose ``body``
ends up empty or whitespace-only — typically aborted creations,
clipboard-paste-to-clear, or watch-folder picks that drained the file
contents before the row landed. They're not user-meaningful and they
clutter every notes listing.

This module is the one-shot core that the daily worker
(:mod:`app.workers.stale_note_pruner_worker`) and the "Run Now" admin
route (:mod:`app.web.routes.stale_note_pruner`) both call.

Algorithm
---------
1. Scan ``notes`` for rows where ``body IS NULL OR length(trim(body))
   = 0``, ``deleted_at IS NULL``, and ``created_at <
   DATE('now', '-<N> days')``.
2. If ``dry_run`` is ``True`` (the default — safety first): return the
   candidate count + age threshold without touching anything.
3. Otherwise stamp ``deleted_at = datetime('now')`` on every matched
   row in one parametrised UPDATE.

Defaults
--------
* ``min_age_days = 90`` — long enough that a user who quickly drafted
  an empty note and walked away for a couple of months still has a
  chance to come back and fill it in, but short enough that the
  pruner is doing useful trim once a day. Tunable via the function arg
  or (via the worker) the ``stale_note_pruner_min_age_days`` kv row
  if/when added; the v1.49 cut keeps the default hard-coded.

Safety
------
* All SQL is parametrised.
* The default mode is ``dry_run=True``; the operator UI button and the
  worker explicitly pass ``dry_run=False`` to take action. This way an
  accidental import-and-call from a notebook never silently nukes
  anything.
* The pruner sets ``deleted_at`` — it never executes ``DELETE``. The
  rows are still in the table and can be revived by clearing the
  column. The recycle-bin job is a separate, opt-in concern.
* The default of the kv toggle in the worker is ``"0"`` (off) — this
  feature is destructive enough that the operator must consciously
  enable it from the admin page.
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.stale_note_pruner")


class StaleNoteCandidate(TypedDict):
    """One row returned by :func:`find_stale_notes`.

    Only the fields the admin UI's preview list needs are projected —
    the body is by definition empty/whitespace so we deliberately do
    not echo it.
    """

    id: int
    created_at: str


class PruneResult(TypedDict):
    """Return shape of :func:`prune_stale`.

    ``count`` is the number of rows the run *would have* (or did)
    soft-delete; ``age_threshold_days`` is echoed so the caller doesn't
    have to remember which cutoff was used (the worker may pass a
    non-default value in the future).
    """

    count: int
    age_threshold_days: int
    dry_run: bool


# Default age cutoff in days. Public so callers + tests can reference
# the same constant rather than hard-coding the literal.
DEFAULT_MIN_AGE_DAYS: int = 90

# Cap on the age the caller may pass in. Years are fine; negative
# values or absurdly small windows (which would soft-delete fresh
# notes) collapse to the default. The route + worker both go through
# here so a fat-finger from the settings UI cannot blast the table.
_MIN_AGE_FLOOR: int = 1
_MIN_AGE_CEILING: int = 36_500  # 100 years — sanity cap, not a policy

# SQL fragment shared by the find + update paths. Centralised so the
# definition of "stale" stays in one place; otherwise the two queries
# could drift apart and the preview number wouldn't match the prune
# number.
#
# ``length(trim(body)) = 0`` covers the whitespace-only case
# (spaces, tabs, newlines). ``body IS NULL`` covers the NULL case
# explicitly — SQLite would short-circuit ``length(trim(NULL))`` to
# NULL which is falsy, but being explicit makes the intent obvious.
_STALE_PREDICATE: str = (
    "(body IS NULL OR length(trim(body)) = 0) "
    "AND deleted_at IS NULL "
    "AND created_at < DATE(?, ?)"
)


def _coerce_age(min_age_days: int) -> int:
    """Clamp ``min_age_days`` into ``[_MIN_AGE_FLOOR, _MIN_AGE_CEILING]``.

    Out-of-range values collapse to :data:`DEFAULT_MIN_AGE_DAYS` so a
    bad kv row or a fat-finger in the UI cannot ever wipe fresh notes.
    """
    try:
        value = int(min_age_days)
    except (TypeError, ValueError):
        log.warning("stale_note_pruner.age.invalid", raw=str(min_age_days))
        return DEFAULT_MIN_AGE_DAYS
    if value < _MIN_AGE_FLOOR or value > _MIN_AGE_CEILING:
        log.warning("stale_note_pruner.age.out_of_range", value=value)
        return DEFAULT_MIN_AGE_DAYS
    return value


def _row_to_candidate(row: Any) -> StaleNoteCandidate:
    """Project one aiosqlite ``Row`` into a :class:`StaleNoteCandidate`."""
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
    }


async def find_stale_notes(
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> list[StaleNoteCandidate]:
    """Return the rows the pruner would soft-delete at the given cutoff.

    Parameters
    ----------
    min_age_days:
        How old a row must be (by ``created_at``) before it counts as
        stale. Clamped via :func:`_coerce_age` to a safe range; values
        outside it fall back to :data:`DEFAULT_MIN_AGE_DAYS`.

    Returns
    -------
    list of dict
        One :class:`StaleNoteCandidate` per matching row, ordered
        oldest-first. Empty list when nothing matches — callers can
        treat ``len(...) == 0`` as "no action needed".

    Notes
    -----
    The SQL uses SQLite's ``DATE(?, ?)`` modifier with two parameters
    so the entire expression — including the offset — is bound, not
    interpolated. The first parameter is the literal ``'now'`` anchor;
    the second is ``'-<N> days'`` built from the clamped age. This is
    parametrised end-to-end: an injection in either slot would fail
    to parse as a date and SQLite would return NULL (matching nothing).
    """
    age = _coerce_age(min_age_days)
    offset = f"-{age} days"
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, created_at FROM notes "  # noqa: S608 — _STALE_PREDICATE is a module-level constant, not user input
            f"WHERE {_STALE_PREDICATE} "
            "ORDER BY created_at ASC",
            ("now", offset),
        )
        rows = await cursor.fetchall()
    candidates = [_row_to_candidate(row) for row in rows]
    log.info(
        "stale_note_pruner.scan",
        candidates=len(candidates),
        age_threshold_days=age,
    )
    return candidates


async def prune_stale(
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    dry_run: bool = True,
) -> PruneResult:
    """Soft-delete the stale notes older than ``min_age_days``.

    Parameters
    ----------
    min_age_days:
        Age cutoff in days. See :func:`find_stale_notes`.
    dry_run:
        When ``True`` (the safe default), no rows are mutated — the
        function reports how many *would* be soft-deleted. When
        ``False``, every matching row gets ``deleted_at =
        datetime('now')`` in a single UPDATE.

    Returns
    -------
    dict
        ``{"count": int, "age_threshold_days": int, "dry_run": bool}``.
        ``count`` is the number of rows touched (or that would be
        touched, for dry-run). The worker passes ``dry_run=False``; the
        admin "Preview" button calls :func:`find_stale_notes` directly,
        and the "Prune" button calls this with ``dry_run=False``.

    Notes
    -----
    We deliberately do the count and the UPDATE in the same connection
    session for the real-run path. Two reasons: (1) the count is then
    accurate even under concurrent inserts — SQLite's default
    serialisable behaviour means the UPDATE sees the same row set the
    COUNT did; and (2) it's one round-trip cheaper. For dry-runs we
    skip the UPDATE entirely.
    """
    age = _coerce_age(min_age_days)
    offset = f"-{age} days"
    async with get_connection() as conn:
        cursor = await conn.execute(
            # The interpolated predicate is a module-level constant, not
            # user input — there is no injection vector here.
            f"SELECT COUNT(*) AS n FROM notes WHERE {_STALE_PREDICATE}",  # noqa: S608
            ("now", offset),
        )
        count_row = await cursor.fetchone()
        count = int(count_row["n"]) if count_row else 0

        if not dry_run and count > 0:
            await conn.execute(
                "UPDATE notes SET deleted_at = datetime('now') "  # noqa: S608 — _STALE_PREDICATE is a module-level constant
                f"WHERE {_STALE_PREDICATE}",
                ("now", offset),
            )
            await conn.commit()

    log.info(
        "stale_note_pruner.prune",
        count=count,
        age_threshold_days=age,
        dry_run=dry_run,
    )
    return {
        "count": count,
        "age_threshold_days": age,
        "dry_run": dry_run,
    }


__all__ = [
    "DEFAULT_MIN_AGE_DAYS",
    "PruneResult",
    "StaleNoteCandidate",
    "find_stale_notes",
    "prune_stale",
]
