"""Per-shot OCR re-run revision log + diff helper (v1.46).

Migration 122 added :data:`screenshots.ocr_rerun_count`; the manual
``/api/screenshot/{id}/ocr-rerun`` route lets the operator re-process a
single shot through the OCR pipeline on demand. Until v1.46 the *prior*
``ocr_text`` value was overwritten in place with no audit trail — there
was no way to inspect "what did OCR see last time vs this time".

This module (paired with migration 126) introduces an append-only
revision log keyed to OCR re-extractions. Each re-run writes **two**
rows: one capturing the prior text (``initial`` on the first re-run,
``rerun`` thereafter) and one capturing the freshly extracted text
(always ``rerun``). The diff viewer at
``/screenshot/{id}/ocr-history`` then renders any pair of revisions
side by side via :func:`difflib.unified_diff`.

Distinct from :mod:`app.ocr_history` (v0.92 — snapshots of
*find-and-replace* / *vision-replace* edits with a revert button): that
module is keyed to bulk text edits and writes into the legacy
``ocr_history`` table. This module is keyed to automatic OCR
re-extractions and writes into the v1.46 ``ocr_rerun_history`` table.
Different write paths, different consumers, deliberately separate
tables.

Design contract
---------------
* **Append-only writes.** No ``UPDATE`` / ``DELETE`` anywhere in this
  module. Retention pruning, if ever needed, must be an explicit
  out-of-band job.
* **Parametrised SQL.** All inserts / selects use ``?`` placeholders so
  even a malicious ``run_source`` string would round-trip as data, not
  code (the CHECK constraint in the migration is the second line of
  defence).
* **structlog audit trail.** Each call fires a structured log line
  under ``persona.ocr_rerun_history`` so an operator grepping a runaway
  re-run loop can reconstruct the revision timeline without consulting
  the DB directly.
* **Pure stdlib diff.** :func:`compute_diff` is a thin wrapper over
  :func:`difflib.unified_diff` — diff math is CPU-bound and tiny, no
  reason to pull in an external dependency.
"""

from __future__ import annotations

import difflib
from typing import Final, Literal, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr_rerun_history")

# The set of legal ``run_source`` values, mirrored exactly from the
# CHECK constraint in migration 126. We expose this so callers can
# validate before sending the bind to SQLite — a CHECK violation would
# raise a generic ``sqlite3.IntegrityError`` and hide the real bug.
RunSource = Literal["initial", "rerun", "manual"]
_VALID_RUN_SOURCES: Final[frozenset[str]] = frozenset({"initial", "rerun", "manual"})


# Cap on how many revision rows :func:`list_revisions` will ever return
# in a single call. A pathological re-run loop could in principle
# generate hundreds of rows; the list view paginates implicitly by only
# showing the top page, but the ceiling protects any future internal
# caller from materialising an unbounded list.
_LIST_HARD_CAP: Final[int] = 200


class RevisionRow(TypedDict):
    """Public projection of one row in ``ocr_rerun_history``."""

    id: int
    screenshot_id: int
    ocr_text: str
    char_count: int
    run_at: str
    run_source: str


class DiffResult(TypedDict):
    """Outcome of :func:`compute_diff` — both structured and stringified.

    Attributes:
        lines: Raw ``unified_diff`` output as a list of lines (the
            ``--- a / +++ b / @@ ... @@ / -old / +new`` shape). Empty
            when the two inputs are byte-equal.
        additions: Number of insertion lines (those starting with
            ``"+"`` but not the ``"+++"`` header).
        deletions: Number of deletion lines (those starting with
            ``"-"`` but not the ``"---"`` header).
        unified_diff_str: ``lines`` joined with newlines for direct
            insertion into a ``<pre>`` block. Empty when the inputs are
            identical.
    """

    lines: list[str]
    additions: int
    deletions: int
    unified_diff_str: str


async def record_ocr_revision(
    shot_id: int,
    ocr_text: str,
    run_source: str = "rerun",
) -> int:
    """Append one revision snapshot and return the new ``id``.

    Args:
        shot_id: ``screenshots.id`` whose revision we're recording.
        ocr_text: Full OCR text snapshot to persist. May be the empty
            string — an OCR pass that yielded zero characters is itself
            a forensically interesting data point.
        run_source: Provenance tag — one of ``"initial"``, ``"rerun"``,
            or ``"manual"``. Validated against :data:`_VALID_RUN_SOURCES`
            so an upstream typo surfaces as a :class:`ValueError` here
            instead of a generic SQLite CHECK violation later.

    Returns:
        The freshly-inserted ``ocr_rerun_history.id``.

    Raises:
        ValueError: ``run_source`` is not one of the three legal values.
    """
    if run_source not in _VALID_RUN_SOURCES:
        msg = (
            f"run_source must be one of {sorted(_VALID_RUN_SOURCES)!r}, "
            f"got {run_source!r}"
        )
        log.warning(
            "ocr_rerun_history.invalid_run_source",
            shot_id=int(shot_id),
            run_source=run_source,
        )
        raise ValueError(msg)

    char_count = len(ocr_text)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO ocr_rerun_history "
            "(screenshot_id, ocr_text, char_count, run_source) "
            "VALUES (?, ?, ?, ?)",
            (int(shot_id), ocr_text, char_count, run_source),
        )
        await conn.commit()
        new_id = cursor.lastrowid

    # ``lastrowid`` is documented to be non-None right after a successful
    # INSERT on a non-WITHOUT-ROWID table, but mypy --strict cannot
    # narrow that — we coerce defensively and fall back to ``-1`` so the
    # type stays ``int`` without ever silently corrupting a real id.
    resolved_id = int(new_id) if new_id is not None else -1

    log.info(
        "ocr_rerun_history.recorded",
        shot_id=int(shot_id),
        revision_id=resolved_id,
        run_source=run_source,
        char_count=char_count,
    )
    return resolved_id


async def list_revisions(shot_id: int) -> list[RevisionRow]:
    """Return every recorded revision for ``shot_id`` in newest-first order.

    Ordered by ``run_at DESC`` so the table view places the most
    recent revisions at the top — matches the v0.92 ``ocr_history``
    convention and lets the diff-picker default the "left" side to the
    immediately-prior revision.

    Hard-capped at :data:`_LIST_HARD_CAP` so a runaway re-run loop
    cannot materialise as a megabyte JSON payload to the browser.
    """
    rows: list[RevisionRow] = []
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, screenshot_id, ocr_text, char_count, run_at, run_source "
            "FROM ocr_rerun_history "
            "WHERE screenshot_id = ? "
            "ORDER BY run_at DESC, id DESC "
            "LIMIT ?",
            (int(shot_id), _LIST_HARD_CAP),
        )
        async for row in cursor:
            raw_text = row["ocr_text"]
            rows.append(
                RevisionRow(
                    id=int(row["id"]),
                    screenshot_id=int(row["screenshot_id"]),
                    ocr_text="" if raw_text is None else str(raw_text),
                    char_count=int(row["char_count"]),
                    run_at=str(row["run_at"]),
                    run_source=str(row["run_source"]),
                )
            )

    log.info(
        "ocr_rerun_history.listed",
        shot_id=int(shot_id),
        count=len(rows),
    )
    return rows


async def _fetch_one_revision(revision_id: int) -> RevisionRow | None:
    """Read a single revision row by id, or ``None`` when missing.

    Private because the public API for fetching is :func:`list_revisions`
    — singleton fetches are only useful inside :func:`compute_diff`.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, screenshot_id, ocr_text, char_count, run_at, run_source "
            "FROM ocr_rerun_history "
            "WHERE id = ?",
            (int(revision_id),),
        )
        row = await cursor.fetchone()

    if row is None:
        return None

    raw_text = row["ocr_text"]
    return RevisionRow(
        id=int(row["id"]),
        screenshot_id=int(row["screenshot_id"]),
        ocr_text="" if raw_text is None else str(raw_text),
        char_count=int(row["char_count"]),
        run_at=str(row["run_at"]),
        run_source=str(row["run_source"]),
    )


async def compute_diff(rev_id_a: int, rev_id_b: int) -> DiffResult:
    """Compute a unified diff between two revisions.

    Both revisions are looked up by id; a missing row yields the empty
    string for that side so the diff still renders and the caller can
    surface a "(revision deleted)" notice in the UI without a separate
    existence probe.

    Args:
        rev_id_a: ``ocr_rerun_history.id`` of the "from" (old) side.
        rev_id_b: ``ocr_rerun_history.id`` of the "to" (new) side.

    Returns:
        A :class:`DiffResult` with both the structured line list and a
        ready-to-render unified-diff string. ``additions`` / ``deletions``
        count true content lines, excluding the ``"+++"`` / ``"---"``
        file headers so a no-op diff reports ``0`` of each.
    """
    rev_a = await _fetch_one_revision(rev_id_a)
    rev_b = await _fetch_one_revision(rev_id_b)

    text_a = rev_a["ocr_text"] if rev_a is not None else ""
    text_b = rev_b["ocr_text"] if rev_b is not None else ""

    label_a = f"revision {rev_id_a}"
    label_b = f"revision {rev_id_b}"

    # ``splitlines()`` strips the trailing newline for us; we pass
    # ``lineterm=""`` so :func:`difflib.unified_diff` does not re-add
    # one and the joined string round-trips faithfully through a
    # ``<pre>`` block.
    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()

    diff_lines = list(
        difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=label_a,
            tofile=label_b,
            lineterm="",
        )
    )

    # Count true add/del lines — the headers emitted by ``unified_diff``
    # are ``"--- <label>"`` and ``"+++ <label>"`` and must not inflate
    # the totals. A pure content line starts with a single ``-`` / ``+``
    # not followed by another of the same character.
    additions = sum(
        1
        for line in diff_lines
        if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1
        for line in diff_lines
        if line.startswith("-") and not line.startswith("---")
    )

    unified_diff_str = "\n".join(diff_lines)

    log.info(
        "ocr_rerun_history.diff_computed",
        rev_id_a=int(rev_id_a),
        rev_id_b=int(rev_id_b),
        diff_lines=len(diff_lines),
        additions=additions,
        deletions=deletions,
        rev_a_missing=rev_a is None,
        rev_b_missing=rev_b is None,
    )

    return DiffResult(
        lines=diff_lines,
        additions=additions,
        deletions=deletions,
        unified_diff_str=unified_diff_str,
    )


__all__ = [
    "DiffResult",
    "RevisionRow",
    "RunSource",
    "compute_diff",
    "list_revisions",
    "record_ocr_revision",
]
