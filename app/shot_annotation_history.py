"""Screenshot annotation revision history — autosave timeline (v1.22).

The annotation editor in :mod:`app.shot_annotations` keeps ONE live row
per screenshot and overwrites it on every save. v1.22 adds a 2-second
debounced *autosave* on the editor page so a browser crash never wipes
in-progress work. Every autosave (and every explicit Save click) ALSO
appends an immutable revision row here, exposing a revert-to-earlier-
state timeline for power users.

Design contract
---------------
* **Append-only writes.** Nothing here ever ``UPDATE``s a revision row;
  the live state lives in ``shot_annotation`` (managed by
  :mod:`app.shot_annotations`). Pruning the autosave tail is a single
  ``DELETE`` keyed by ``screenshot_id`` + ``source = 'autosave'`` so a
  ``manual`` save is never sacrificed for an autosave cap.
* **Capped retention.** :func:`record_revision` keeps at most
  :data:`MAX_AUTOSAVES_PER_SHOT` autosave rows per screenshot. Manual
  saves are retained unconditionally — they are explicit user intent.
* **SVG sanitisation reuse.** We funnel every payload through
  :func:`app.shot_annotations.sanitise_svg` before it touches the DB so
  a malicious ``<script>`` blob from a tampered client cannot land in
  the revision table either. The live upsert path already sanitises on
  read; revisions inherit the same contract on write.
* **Parametrised SQL.** Every insert / delete uses ``?`` placeholders.
* **structlog audit trail.** Each call emits a structured log line
  under ``persona.shot_annotation_history`` so an operator grepping for
  a stuck autosave loop can reconstruct the timeline without consulting
  the DB directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict, cast

from app.logging_setup import get_logger
from app.shot_annotations import MAX_PAYLOAD_BYTES, sanitise_svg
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.shot_annotation_history")

MAX_AUTOSAVES_PER_SHOT: int = 20
"""How many ``source='autosave'`` rows we keep per screenshot.

The newest 20 are retained; older autosaves are pruned on every insert.
Manual saves are NOT counted against this cap.
"""

RevisionSource = Literal["autosave", "manual"]
"""Mirror of the ``CHECK`` enum in migration 120."""


class RevisionRow(TypedDict):
    """One row from ``shot_annotation_revision`` exposed to callers."""

    id: int
    screenshot_id: int
    svg_payload: str
    saved_at: str
    source: str


def _validate_source(source: str) -> RevisionSource:
    """Reject anything the DB ``CHECK`` would reject — but loudly here.

    The DB will raise ``IntegrityError`` on an unknown ``source`` but
    the error message is opaque; surfacing the rejection at the Python
    boundary gives the caller a useful traceback.
    """
    if source not in ("autosave", "manual"):
        msg = f"invalid revision source: {source!r}"
        raise ValueError(msg)
    return cast("RevisionSource", source)


def _validate_payload_size(svg_payload: str) -> None:
    """Reject payloads larger than the live-annotation byte cap.

    Mirrors :data:`app.shot_annotations.MAX_PAYLOAD_BYTES` so the
    revision table cannot grow rows the live upsert path would refuse.
    """
    size = len(svg_payload.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        msg = f"svg_payload too large: {size} bytes (max {MAX_PAYLOAD_BYTES})"
        raise ValueError(msg)


def _row_to_dict(row: aiosqlite.Row) -> RevisionRow:
    return {
        "id": int(row["id"]),
        "screenshot_id": int(row["screenshot_id"]),
        "svg_payload": str(row["svg_payload"]),
        "saved_at": str(row["saved_at"]),
        "source": str(row["source"]),
    }


async def _prune_autosave_tail(
    conn: aiosqlite.Connection,
    screenshot_id: int,
    keep: int,
) -> int:
    """Delete autosave rows older than the newest ``keep`` for this shot.

    Manual saves are *not* touched — only ``source = 'autosave'`` rows
    are eligible for pruning. Returns the number of rows removed (handy
    for the structlog line at the call site).
    """
    cursor = await conn.execute(
        """
        DELETE FROM shot_annotation_revision
        WHERE id IN (
            SELECT id FROM shot_annotation_revision
            WHERE screenshot_id = ? AND source = 'autosave'
            ORDER BY saved_at DESC, id DESC
            LIMIT -1 OFFSET ?
        )
        """,
        (int(screenshot_id), int(keep)),
    )
    return int(cursor.rowcount or 0)


async def record_revision(
    shot_id: int,
    svg_payload: str,
    source: str = "autosave",
) -> int:
    """Append one revision row for ``shot_id``. Returns the new row id.

    The payload is sanitised via :func:`app.shot_annotations.sanitise_svg`
    before the insert so a tampered client cannot smuggle a ``<script>``
    blob into the revision table. Raises :class:`ValueError` on an
    invalid ``source`` or an oversized payload.

    After insert, autosave-source rows older than the newest
    :data:`MAX_AUTOSAVES_PER_SHOT` are pruned for this shot. Manual rows
    are never pruned by this function.
    """
    validated_source = _validate_source(source)
    _validate_payload_size(svg_payload)
    cleaned = sanitise_svg(svg_payload)

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO shot_annotation_revision
                (screenshot_id, svg_payload, source)
            VALUES (?, ?, ?)
            """,
            (int(shot_id), cleaned, validated_source),
        )
        new_id = int(cursor.lastrowid or 0)
        pruned = 0
        if validated_source == "autosave":
            pruned = await _prune_autosave_tail(
                conn, shot_id, MAX_AUTOSAVES_PER_SHOT
            )
        await conn.commit()

    log.info(
        "shot_annotation_history.record",
        shot_id=int(shot_id),
        revision_id=new_id,
        source=validated_source,
        bytes=len(cleaned.encode("utf-8")),
        pruned=pruned,
    )
    return new_id


async def list_revisions(
    shot_id: int,
    limit: int = MAX_AUTOSAVES_PER_SHOT,
) -> list[RevisionRow]:
    """Return the newest revisions for ``shot_id`` (newest first).

    Hard-capped at :data:`MAX_AUTOSAVES_PER_SHOT` regardless of the
    caller's ``limit`` so an off-by-one in a future UI cannot materialise
    an unbounded list. Returns both autosave and manual rows interleaved
    by ``saved_at`` so the timeline UI shows them in real chronological
    order.
    """
    effective_limit = max(1, min(int(limit), MAX_AUTOSAVES_PER_SHOT))
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, screenshot_id, svg_payload, saved_at, source
            FROM shot_annotation_revision
            WHERE screenshot_id = ?
            ORDER BY saved_at DESC, id DESC
            LIMIT ?
            """,
            (int(shot_id), effective_limit),
        )
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def get_revision(revision_id: int) -> RevisionRow | None:
    """Fetch one revision row by id, or ``None`` if it does not exist.

    Used by the restore endpoint to look up the payload before piping it
    back through :func:`app.shot_annotations.upsert_annotation`.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, screenshot_id, svg_payload, saved_at, source
            FROM shot_annotation_revision
            WHERE id = ?
            """,
            (int(revision_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)
