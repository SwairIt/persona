"""Retroactive tag canonicaliser — collapse spelling variants of one concept.

The pre-store overlay in :mod:`app.tag_aliases` rewrites *future* tag
writes before they hit the ``tags`` row, but it cannot touch the legacy
rows already split across case-different (``StandUp`` vs ``stand-up``),
whitespace-different (``stand up`` vs ``stand_up``) and unicode-
different (NFC ``café`` vs NFD ``café``) variants of the same
concept. Each variant survives in the database as a separate
``tags`` row with its own screenshot set, so the search UI buckets one
concept across N facets and the operator has no convenient way to
consolidate them without manually running :func:`app.tag_merge.merge_tags`
N-1 times per cluster.

This module is the retroactive sweep that does the consolidation in
one shot. It:

* scans every distinct tag name that currently labels at least
  ``min_count`` screenshots,
* normalises each name with :func:`normalize` (strip + lowercase +
  NFKC + collapse whitespace to underscores),
* groups raw names by their normalised form,
* for every cluster that contains more than one raw spelling, picks
  the spelling with the highest screenshot-count as the canonical and
  proposes the remaining spellings as aliases to be folded into it,
* on ``apply_canonicalisation(dry_run=False)`` records the proposed
  ``alias -> canonical`` rows in the ``tag_alias`` audit table and
  rewrites the ``tags`` rows so the canonical owns every screenshot
  the cluster ever touched.

Semantics
---------
* ``normalize`` is deterministic and idempotent — ``normalize(normalize(x))
  == normalize(x)`` — so the alias map is stable across calls.
* Only clusters with **more than one distinct raw spelling** are
  surfaced: a single-spelling cluster is its own canonical and needs
  no rewrite.
* ``apply_canonicalisation`` rewrites at the ``tags`` row level. For
  every ``alias != canonical`` it:

  1. inserts a ``tag_alias`` audit row,
  2. re-points every ``screenshot_tags`` link to the canonical tag id
     (skipping pairs that already exist on the canonical side via
     ``INSERT OR IGNORE``),
  3. deletes the now-orphan source-side links and the source ``tags``
     row.

  The whole rewrite for one alias runs inside a single transaction so
  a mid-cluster crash never leaves a half-merged state on disk.
* The ``tag_alias`` table is shared with :mod:`app.tag_aliases` (the
  pre-store overlay reads it via ``SELECT canonical FROM tag_alias
  WHERE alias = ?``), so a once-canonicalised legacy spelling stays
  rewritten on every future write too — the retroactive sweep and the
  pre-store overlay reinforce each other.
"""

from __future__ import annotations

import unicodedata
from typing import TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_canonicaliser")

# Default threshold for :func:`build_alias_map`. A tag that labels only
# a single screenshot is almost always a one-off mis-tag the operator
# would rather see surfaced in :mod:`app.tag_cleanup`; the canonicaliser
# focuses on clusters that already have non-trivial usage so a rewrite
# is worth the audit-trail row.
_DEFAULT_MIN_COUNT = 2


class AliasEntry(TypedDict):
    """One raw spelling inside a cluster."""

    alias: str
    count: int


class ClusterPreview(TypedDict):
    """Dry-run preview for one cluster — canonical + aliases that would fold in."""

    canonical: str
    canonical_count: int
    aliases: list[AliasEntry]


class ApplyResult(TypedDict):
    """Outcome of :func:`apply_canonicalisation`.

    Attributes:
        clusters: Number of clusters that contained more than one raw
            spelling. For ``dry_run=True`` this is the cluster count
            the preview would expose; for ``dry_run=False`` it is the
            number of clusters whose canonical winner was actually
            applied.
        rows_updated: Total number of ``screenshot_tags`` rows that
            were re-pointed (or, in dry-run mode, *would* be re-
            pointed) onto a canonical tag id. Duplicate (screenshot,
            canonical) pairs that the ``INSERT OR IGNORE`` step drops
            are still counted — the destination owns the screenshot
            either way.
        dry_run: Echoes the ``dry_run`` argument so the caller can
            tell preview output from real output at a glance.
        preview: Per-cluster breakdown. Present in both modes so the
            HTMX UI can render the same table for "what would happen"
            and "what just happened".
    """

    clusters: int
    rows_updated: int
    dry_run: bool
    preview: list[ClusterPreview]


def normalize(raw: str) -> str:
    """Return the canonical-form key for ``raw``.

    The pipeline is:

    1. Coerce to ``str`` and ``strip`` outer whitespace.
    2. NFKC-normalise so visually-identical-but-codepoint-different
       glyphs (full-width ASCII, combining accents, ligatures) collapse
       to the same key.
    3. ``casefold`` (stronger than ``lower`` — handles ``ß`` → ``ss``
       and other locale-sensitive folds correctly).
    4. Collapse every run of ASCII or Unicode whitespace plus the
       common punctuation separators (``-``, ``_``, ``/``) to a single
       ``_``. We treat hyphens and slashes as whitespace because
       operators routinely write the same concept as ``stand-up``,
       ``stand up`` or ``stand/up`` interchangeably.
    5. Strip leading / trailing ``_`` so a name that started or ended
       with whitespace does not produce a bracketing underscore.

    Examples
    --------
    >>> normalize("  Stand Up  ")
    'stand_up'
    >>> normalize("stand-up")
    'stand_up'
    >>> normalize("STAND_UP")
    'stand_up'
    >>> # Full-width Latin letters NFKC-fold to their ASCII counterparts,
    >>> # so a tag typed on a Japanese IME collapses to the same key.
    """
    text = raw.strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    out: list[str] = []
    prev_sep = False
    for ch in text:
        if ch.isspace() or ch in ("-", "_", "/"):
            if not prev_sep:
                out.append("_")
                prev_sep = True
            continue
        out.append(ch)
        prev_sep = False
    return "".join(out).strip("_")


async def _fetch_tag_counts(
    conn: aiosqlite.Connection,
    min_count: int,
) -> list[tuple[str, int]]:
    """Return ``(name, count)`` for every tag with at least ``min_count`` uses.

    The spec frames the query as
    ``SELECT tag, COUNT(*) FROM screenshot_tags GROUP BY tag``, but the
    real schema (see :file:`001_tags.sql`) joins ``tags.name`` with the
    ``screenshot_tags`` link table via ``tag_id``. The semantics are
    identical: one row per distinct tag name with the number of
    screenshots currently labelled with it. Tags that have zero
    assignments (``LEFT JOIN`` would surface them) are deliberately
    excluded — there is nothing to re-point and the cluster would
    consist of a single zero-count winner.
    """
    cursor = await conn.execute(
        """
        SELECT t.name AS name, COUNT(st.tag_id) AS n
          FROM tags AS t
          JOIN screenshot_tags AS st ON st.tag_id = t.id
         GROUP BY t.name
        HAVING COUNT(st.tag_id) >= ?
         ORDER BY t.name ASC
        """,
        (int(min_count),),
    )
    rows = await cursor.fetchall()
    return [(str(row["name"]), int(row["n"])) for row in rows]


async def build_alias_map(
    min_count: int = _DEFAULT_MIN_COUNT,
) -> dict[str, list[AliasEntry]]:
    """Group existing tag names by their normalised form.

    Returns a mapping ``{canonical_raw_name: [{"alias": str, "count":
    int}, ...]}`` containing **only** clusters with more than one raw
    spelling — single-spelling clusters need no rewrite and are
    omitted. The canonical winner for each cluster is the raw spelling
    with the highest ``count``; ties are broken alphabetically so the
    choice is deterministic across runs.

    The returned ``aliases`` list contains every raw spelling in the
    cluster *except* the canonical — those are the names that
    :func:`apply_canonicalisation` would fold into the canonical.

    Parameters
    ----------
    min_count:
        Filter applied at the SQL level: a tag is only considered when
        it labels at least this many screenshots. Default ``2`` skips
        one-off mis-tags so the preview surfaces consolidation
        opportunities the operator actually cares about.
    """
    min_count = max(min_count, 1)
    async with get_connection() as conn:
        rows = await _fetch_tag_counts(conn, min_count)

    clusters: dict[str, list[AliasEntry]] = {}
    for name, count in rows:
        key = normalize(name)
        if not key:
            continue
        clusters.setdefault(key, []).append({"alias": name, "count": count})

    out: dict[str, list[AliasEntry]] = {}
    for variants in clusters.values():
        if len(variants) < 2:
            continue
        # Pick canonical = highest count, alphabetical tiebreak so the
        # winner is stable across re-runs even when two spellings tie.
        sorted_variants = sorted(
            variants,
            key=lambda v: (-v["count"], v["alias"]),
        )
        canonical = sorted_variants[0]["alias"]
        aliases = sorted_variants[1:]
        out[canonical] = aliases

    log.info(
        "tag_canonicaliser.build_alias_map",
        min_count=min_count,
        total_tags=len(rows),
        clusters=len(out),
    )
    return out


async def _lookup_tag_id(
    conn: aiosqlite.Connection,
    name: str,
) -> int | None:
    """Return the id of the tag named ``name`` or ``None`` if missing."""
    cursor = await conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return int(row["id"])


async def _count_assignments(
    conn: aiosqlite.Connection,
    tag_id: int,
) -> int:
    """Return the screenshot-link count for ``tag_id``."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshot_tags WHERE tag_id = ?",
        (tag_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _fold_one_alias(
    conn: aiosqlite.Connection,
    alias: str,
    canonical: str,
) -> int:
    """Merge ``alias`` into ``canonical`` and write the audit row.

    Returns the number of source-side ``screenshot_tags`` rows that
    were consumed by the merge. Runs inside its own transaction so a
    failure on one alias never poisons the rest of the cluster.
    """
    moved = 0
    try:
        await conn.execute("BEGIN")
        # The pre-store overlay (:mod:`app.tag_aliases`) reads the same
        # ``tag_alias`` table to rewrite future writes, so recording the
        # row here makes the canonicalisation stick across subsequent
        # tag-writes too. ``INSERT OR IGNORE`` lets us re-apply the
        # canonicaliser safely when the operator re-introduces a legacy
        # spelling that was already folded once.
        await conn.execute(
            """
            INSERT OR IGNORE INTO tag_alias (alias, canonical)
            VALUES (?, ?)
            """,
            (alias, canonical),
        )

        alias_id = await _lookup_tag_id(conn, alias)
        canonical_id = await _lookup_tag_id(conn, canonical)
        if alias_id is None or canonical_id is None:
            # Either tag vanished between :func:`build_alias_map` and
            # now (concurrent edit). Audit row is still useful as a
            # historical record; nothing to re-point.
            await conn.commit()
            return 0
        if alias_id == canonical_id:  # pragma: no cover — defensive
            await conn.commit()
            return 0

        # Copy every (screenshot, alias) link to the canonical side,
        # skipping pairs that the canonical already has.
        await conn.execute(
            """
            INSERT OR IGNORE INTO screenshot_tags (screenshot_id, tag_id, created_at)
            SELECT screenshot_id, ?, created_at
              FROM screenshot_tags
             WHERE tag_id = ?
            """,
            (canonical_id, alias_id),
        )
        moved = await _count_assignments(conn, alias_id)
        # Drop the source-side links so the alias tag row is safe to
        # delete (the PK / FK would otherwise block the next step).
        await conn.execute(
            "DELETE FROM screenshot_tags WHERE tag_id = ?",
            (alias_id,),
        )
        await conn.execute("DELETE FROM tags WHERE id = ?", (alias_id,))
        await conn.commit()
    except aiosqlite.Error:
        await conn.rollback()
        log.exception(
            "tag_canonicaliser.fold_failed",
            alias=alias,
            canonical=canonical,
        )
        raise
    return moved


async def apply_canonicalisation(
    dry_run: bool = True,
    min_count: int = _DEFAULT_MIN_COUNT,
) -> ApplyResult:
    """Apply (or preview) the canonicalisation sweep.

    When ``dry_run`` is ``True`` (default) the sweep computes the
    cluster map and returns a per-cluster breakdown without touching
    the database. When ``dry_run`` is ``False`` every ``alias !=
    canonical`` in every cluster is folded into the canonical via
    :func:`_fold_one_alias`: the alias row in ``tags`` is deleted,
    every ``screenshot_tags`` link is re-pointed at the canonical, and
    an audit row is inserted into ``tag_alias``.

    Parameters
    ----------
    dry_run:
        ``True`` to preview only; ``False`` to commit the rewrites.
    min_count:
        Forwarded to :func:`build_alias_map` — only tags with at least
        this many screenshot assignments are considered.

    Returns
    -------
    ApplyResult
        ``clusters`` is the number of multi-spelling clusters that were
        surfaced; ``rows_updated`` is the total number of
        ``screenshot_tags`` rows that were re-pointed (or would be, in
        dry-run mode); ``preview`` is the per-cluster breakdown.
    """
    alias_map = await build_alias_map(min_count=min_count)

    preview: list[ClusterPreview] = []
    rows_updated = 0

    if dry_run:
        # Same cluster map the apply path would consume, plus the
        # per-tag screenshot counts so the UI can render an honest
        # blast-radius estimate.
        for canonical, aliases in alias_map.items():
            canonical_count = await _canonical_count_estimate(canonical)
            preview.append(
                {
                    "canonical": canonical,
                    "canonical_count": canonical_count,
                    "aliases": aliases,
                }
            )
            rows_updated += sum(a["count"] for a in aliases)
        log.info(
            "tag_canonicaliser.dry_run",
            clusters=len(alias_map),
            rows_updated=rows_updated,
        )
        return {
            "clusters": len(alias_map),
            "rows_updated": rows_updated,
            "dry_run": True,
            "preview": preview,
        }

    async with get_connection() as conn:
        for canonical, aliases in alias_map.items():
            canonical_count_before = 0
            cid = await _lookup_tag_id(conn, canonical)
            if cid is not None:
                canonical_count_before = await _count_assignments(conn, cid)
            for entry in aliases:
                alias = entry["alias"]
                if alias == canonical:
                    continue
                moved = await _fold_one_alias(conn, alias, canonical)
                rows_updated += moved
            preview.append(
                {
                    "canonical": canonical,
                    "canonical_count": canonical_count_before,
                    "aliases": aliases,
                }
            )

    log.info(
        "tag_canonicaliser.applied",
        clusters=len(alias_map),
        rows_updated=rows_updated,
    )
    return {
        "clusters": len(alias_map),
        "rows_updated": rows_updated,
        "dry_run": False,
        "preview": preview,
    }


async def _canonical_count_estimate(canonical: str) -> int:
    """Return the current screenshot-count for ``canonical``.

    Helper used by the dry-run path so the preview can show the
    operator the existing size of the canonical bucket alongside the
    incoming aliases. A canonical that does not yet exist as a tag row
    (rare — the canonical is picked from the same query that produced
    every other row) returns ``0``.
    """
    async with get_connection() as conn:
        tid = await _lookup_tag_id(conn, canonical)
        if tid is None:
            return 0
        return await _count_assignments(conn, tid)


__all__ = [
    "AliasEntry",
    "ApplyResult",
    "ClusterPreview",
    "apply_canonicalisation",
    "build_alias_map",
    "normalize",
]
