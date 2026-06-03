"""Append-only snapshots of ``screenshots.ocr_text`` for undo/revert (v0.92).

Persona v0.92 feature 3/3. Three write paths overwrite ``ocr_text``:

* :mod:`app.ocr_find_replace` — bulk regex find-and-replace (v0.77),
* :mod:`app.web.routes.ocr_vision_replace` — vision-text promotion (v0.75),
* any future per-shot manual edit.

Until v0.92 the previous value was lost the moment the ``UPDATE`` fired
and the operator had no way to revert. This module exposes the
snapshot table created in migration ``076_ocr_history.sql`` as three
deliberately small async primitives:

* :func:`record_snapshot` — append one row capturing ``prev_text``.
  Idempotent in the no-op sense: a ``NULL`` / empty ``prev_text`` skips
  the insert because a revert to "no text" is indistinguishable from
  doing nothing.
* :func:`list_for_shot` — return the snapshot rows for one shot in
  newest-first order so the UI can render a revert list.
* :func:`revert` — restore one snapshot row's ``prev_text`` into the
  live ``screenshots.ocr_text`` column. The restore itself takes a
  fresh snapshot of the about-to-be-overwritten value first, so
  reverting a revert is just a second click on the new top row.

Design contract
---------------
* **Append-only writes.** Nothing here ever ``UPDATE``s or ``DELETE``s
  a history row. Retention pruning, if ever needed, must be an
  explicit out-of-band job.
* **NULL-safe.** A ``record_snapshot`` call with ``prev_text=None`` or
  ``""`` is a silent no-op; nothing in the schema benefits from a row
  whose revert would blank a column that was already blank.
* **Parametrised SQL.** All inserts / updates use ``?`` placeholders
  so even a malicious ``reason`` string never reaches SQLite as code.
* **structlog audit trail.** Each call fires a structured log line
  under ``persona.ocr_history`` so an operator grepping for a runaway
  ``find_replace`` can reconstruct the snapshot timeline without
  consulting the DB directly. The web layer additionally writes to
  :mod:`app.audit` so the security-review trail stays uniform with the
  other privileged write paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.ocr_history")


# Cap on how many history rows :func:`list_for_shot` will ever return
# in a single call. The UI paginates implicitly by only showing the
# latest few, but the ceiling protects any future internal caller from
# materialising an unbounded history list.
_LIST_HARD_CAP = 200


class HistoryRow(TypedDict):
    """Public projection of a row in ``ocr_history``."""

    id: int
    shot_id: int
    prev_text: str
    replaced_at: str
    reason: str | None


class RevertResult(TypedDict):
    """Outcome summary returned by :func:`revert`."""

    history_id: int
    shot_id: int
    restored_chars: int


def _empty_to_none(value: str | None) -> str | None:
    """Normalise empty/whitespace-only strings to ``None``.

    Matches the convention used by :mod:`app.audit` — the DB should
    never hold an ambiguous blank ``reason``.
    """
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


async def record_snapshot(
    shot_id: int,
    prev_text: str | None,
    reason: str | None = None,
) -> int | None:
    """Append a snapshot of ``prev_text`` for ``shot_id``; return the new row id.

    A ``None`` / empty ``prev_text`` is treated as "nothing worth
    reverting to" and the call is a silent no-op returning ``None`` —
    the schema's ``NOT NULL`` constraint on ``prev_text`` would reject
    the insert anyway, and a revert would be indistinguishable from
    doing nothing.

    Args:
        shot_id: ``screenshots.id`` whose ``ocr_text`` is about to be
            overwritten.
        prev_text: The current ``ocr_text`` value, captured *before*
            the caller's own UPDATE fires.
        reason: Free-form slug identifying the write path
            (``"find_replace"``, ``"vision_replace"``, ``"manual"``);
            stored as-is and never parsed.

    Returns:
        The new ``ocr_history.id`` on a successful insert, ``None`` when
        the snapshot was skipped (NULL / empty ``prev_text``).
    """
    if prev_text is None or prev_text == "":
        log.info(
            "ocr_history.snapshot.skip_empty",
            shot_id=shot_id,
            reason=_empty_to_none(reason),
        )
        return None

    cleaned_reason = _empty_to_none(reason)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO ocr_history (shot_id, prev_text, reason) "
            "VALUES (?, ?, ?)",
            (int(shot_id), prev_text, cleaned_reason),
        )
        await conn.commit()
        new_id = cursor.lastrowid

    log.info(
        "ocr_history.snapshot.ok",
        shot_id=int(shot_id),
        history_id=int(new_id) if new_id is not None else None,
        reason=cleaned_reason,
        chars=len(prev_text),
    )
    return int(new_id) if new_id is not None else None


async def list_for_shot(shot_id: int) -> list[HistoryRow]:
    """Return snapshot rows for ``shot_id`` in newest-first order.

    Hard-capped at :data:`_LIST_HARD_CAP` so a runaway find-replace
    history doesn't materialise as a megabyte JSON payload to the
    browser. The UI typically only renders the top handful anyway.
    """
    rows: list[HistoryRow] = []
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, shot_id, prev_text, replaced_at, reason "
            "FROM ocr_history "
            "WHERE shot_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (int(shot_id), _LIST_HARD_CAP),
        )
        async for row in cursor:
            reason_value = row["reason"]
            rows.append(
                HistoryRow(
                    id=int(row["id"]),
                    shot_id=int(row["shot_id"]),
                    prev_text=str(row["prev_text"]),
                    replaced_at=str(row["replaced_at"]),
                    reason=str(reason_value) if reason_value is not None else None,
                )
            )

    log.info(
        "ocr_history.list.ok",
        shot_id=int(shot_id),
        count=len(rows),
    )
    return rows


async def revert(history_id: int) -> RevertResult | None:
    """Restore one snapshot row's ``prev_text`` into the live ``ocr_text``.

    Before the UPDATE fires we take a fresh snapshot (``reason="revert"``)
    of whatever ``ocr_text`` currently holds, so reverting a revert is
    just a second click on the new top row. The ``UPDATE`` triggers
    ``screenshots_au`` and keeps the FTS index consistent automatically.

    Args:
        history_id: ``ocr_history.id`` of the snapshot to restore.

    Returns:
        A :class:`RevertResult` summarising the operation, or ``None``
        when ``history_id`` does not exist (so the route layer can map
        a missing row to 404 without a second query).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, shot_id, prev_text FROM ocr_history WHERE id = ?",
            (int(history_id),),
        )
        snapshot = await cursor.fetchone()
        if snapshot is None:
            log.warning(
                "ocr_history.revert.not_found",
                history_id=int(history_id),
            )
            return None

        shot_id = int(snapshot["shot_id"])
        target_text = str(snapshot["prev_text"])

        # Capture the *current* ocr_text before overwriting it so the
        # operator can revert the revert. We use ``executemany``-shaped
        # parametrisation but inline because this is a single insert,
        # then mirror the standalone :func:`record_snapshot` skip rule
        # for NULL/empty bodies.
        current_cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots WHERE id = ?",
            (shot_id,),
        )
        current_row = await current_cursor.fetchone()
        current_text: str | None = None
        if current_row is not None:
            raw = current_row["ocr_text"]
            current_text = None if raw is None else str(raw)

        if current_text is not None and current_text != "":
            await conn.execute(
                "INSERT INTO ocr_history (shot_id, prev_text, reason) "
                "VALUES (?, ?, ?)",
                (shot_id, current_text, "revert"),
            )

        await conn.execute(
            "UPDATE screenshots SET ocr_text = ? WHERE id = ?",
            (target_text, shot_id),
        )

        # Belt-and-braces FTS refresh: ``screenshots_au`` already keeps
        # ``screenshots_fts`` consistent on UPDATE, but we mirror the
        # explicit rebuild that :mod:`app.ocr_find_replace` issues so a
        # future refactor that drops the trigger doesn't silently rot
        # the revert path.
        await conn.execute(
            "INSERT INTO screenshots_fts(screenshots_fts) VALUES('rebuild')"
        )

        await conn.commit()

    log.info(
        "ocr_history.revert.ok",
        history_id=int(history_id),
        shot_id=shot_id,
        chars=len(target_text),
    )
    return RevertResult(
        history_id=int(history_id),
        shot_id=shot_id,
        restored_chars=len(target_text),
    )


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    """Defensive probe for the ``ocr_history`` table — used only by tests.

    Production code can assume the table exists because ``init_database``
    runs all migrations before serving any request; tests that exercise
    a half-migrated DB can call this helper to bail out gracefully.
    """
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    )
    row = await cursor.fetchone()
    return row is not None


__all__ = [
    "HistoryRow",
    "RevertResult",
    "list_for_shot",
    "record_snapshot",
    "revert",
]
