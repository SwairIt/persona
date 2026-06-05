"""Image-similarity duplicate finder — surface near-dupes the deduper missed.

The capture-time pHash deduper
(``app.dedup.find_or_create_dedup_group``) clusters shots whose pHash
is within a fixed Hamming threshold. Anything beyond that threshold —
e.g. a minor pixel diff, an antialiasing tweak, a moving cursor — gets
its own ``dedup_group_id`` and shows up as a separate row even though
a human would call it the same shot.

This module is the cleanup hammer for that gap. It is a one-off
admin scanner, not part of the live capture path: the operator opens
``/admin/dup-finder``, picks a threshold + lookback, and gets back
suspected-duplicate groups they can bulk-delete from the UI.

Algorithm
---------
1. Load every shot in the lookback window with a non-NULL pHash,
   ordered by ``captured_at``.
2. Slide a 1-hour window over the ordered list and pairwise-compare
   only within the window. Near-dupes that the capture-time deduper
   missed are almost always seconds apart, not days apart, so this
   bounds the comparison count from ``O(N^2)`` to roughly ``O(N*k)``
   where ``k`` is the shots-per-hour density (~50k comparisons for
   10k shots instead of 50M).
3. For every pair with ``hamming_distance(a, b) <= threshold``, union
   them into a candidate group via a simple union-find.
4. Return one entry per multi-member group with a suggested
   "keep" id: the highest pinned tier first (so the user's pin
   survives the bulk delete), then the oldest ``captured_at`` as a
   tiebreaker (so the original capture is the survivor by default).

Pure read path — no writes, no commits. The soft-delete happens via
``POST /api/dup-finder/delete-group`` in the route module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from app.dedup import hamming_distance
from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.dup_finder")

# Sliding-comparison window. Near-dupes the runtime deduper missed are
# overwhelmingly captured within seconds of each other (same app, same
# scroll position, one cursor pixel different); shots an hour apart
# are practically never visually identical even if pHash collides by
# accident. One hour is a comfortable upper bound that keeps the
# inner loop tiny without sacrificing recall in practice.
_WINDOW_SECONDS: Final[int] = 60 * 60

# Pin tier — see migration ``004_tiers.sql``. Rows in the ``pinned``
# tier are the user's explicit "never delete" marker; the suggested
# keep id always prefers them.
_PINNED_TIER: Final[str] = "pinned"


class _UnionFind:
    """Tiny iterative union-find with path compression + union by rank.

    Inlined instead of pulling a dependency: the data set is bounded
    by the lookback window (~tens of thousands of shots) and we only
    need two operations.
    """

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}
        self._rank: dict[int, int] = {}

    def add(self, item: int) -> None:
        """Register ``item`` as its own singleton component if unknown."""
        if item not in self._parent:
            self._parent[item] = item
            self._rank[item] = 0

    def find(self, item: int) -> int:
        """Return the root of ``item``'s component, compressing the path."""
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression — second pass.
        cursor = item
        while self._parent[cursor] != root:
            nxt = self._parent[cursor]
            self._parent[cursor] = root
            cursor = nxt
        return root

    def union(self, left: int, right: int) -> None:
        """Merge the components of ``left`` and ``right``."""
        root_l = self.find(left)
        root_r = self.find(right)
        if root_l == root_r:
            return
        rank_l = self._rank[root_l]
        rank_r = self._rank[root_r]
        if rank_l < rank_r:
            self._parent[root_l] = root_r
        elif rank_l > rank_r:
            self._parent[root_r] = root_l
        else:
            self._parent[root_r] = root_l
            self._rank[root_l] = rank_l + 1

    def components(self) -> dict[int, list[int]]:
        """Group every registered item by root → list of members."""
        groups: dict[int, list[int]] = {}
        for item in self._parent:
            root = self.find(item)
            groups.setdefault(root, []).append(item)
        return groups


async def find_suspected_duplicates(
    threshold: int = 6,
    limit: int = 100,
    lookback_days: int = 30,
) -> dict[str, Any]:
    """Scan the lookback window and return suspected-duplicate groups.

    Args:
        threshold: Hamming distance ceiling. Two shots with a pHash
            distance ``<= threshold`` are considered candidate dupes.
            Defaults to 6 — looser than the runtime deduper's typical
            threshold, on purpose, since the whole point is to catch
            what the deduper missed.
        limit: Maximum number of groups to return. The route uses this
            to cap UI render cost on very dirty databases. Groups are
            returned newest-first by ``first_captured_at``.
        lookback_days: How many days of history to scan. ``30`` keeps
            the scan bounded on long-running installs; the operator
            can crank it up for one-off deep cleans.

    Returns:
        A dict::

            {
                "scanned": int,             # rows pulled from the DB
                "candidates_total": int,    # rows assigned to a group
                "groups": [
                    {
                        "shot_ids": list[int],
                        "count": int,
                        "first_captured_at": str,   # ISO
                        "suggested_keep_id": int,
                    },
                    ...
                ],
            }
    """
    if threshold < 0:
        msg = f"threshold must be >= 0, got {threshold}"
        raise ValueError(msg)
    if limit <= 0:
        return {"scanned": 0, "candidates_total": 0, "groups": []}
    if lookback_days <= 0:
        msg = f"lookback_days must be > 0, got {lookback_days}"
        raise ValueError(msg)

    rows = await _fetch_candidates(lookback_days=lookback_days)
    if not rows:
        log.info(
            "dup_finder.empty",
            threshold=threshold,
            lookback_days=lookback_days,
        )
        return {"scanned": 0, "candidates_total": 0, "groups": []}

    uf = _UnionFind()
    for row in rows:
        uf.add(int(row["id"]))

    pair_count = _build_pairs(rows, threshold=threshold, uf=uf)

    # Component members → group dicts. Filter singletons; they are not
    # dupes, just rows that happened to be in the lookback window.
    by_id = {int(r["id"]): r for r in rows}
    components = uf.components()
    groups: list[dict[str, Any]] = []
    candidates_total = 0
    for members in components.values():
        if len(members) < 2:
            continue
        candidates_total += len(members)
        group_rows = [by_id[mid] for mid in members]
        group_rows.sort(key=lambda r: str(r["captured_at"]))
        keep_id = _pick_keep_id(group_rows)
        groups.append(
            {
                "shot_ids": [int(r["id"]) for r in group_rows],
                "count": len(group_rows),
                "first_captured_at": str(group_rows[0]["captured_at"]),
                "suggested_keep_id": keep_id,
            },
        )

    groups.sort(key=lambda g: g["first_captured_at"], reverse=True)
    groups = groups[:limit]

    log.info(
        "dup_finder.done",
        scanned=len(rows),
        pairs=pair_count,
        groups=len(groups),
        candidates_total=candidates_total,
        threshold=threshold,
        lookback_days=lookback_days,
    )
    return {
        "scanned": len(rows),
        "candidates_total": candidates_total,
        "groups": groups,
    }


async def _fetch_candidates(*, lookback_days: int) -> list[aiosqlite.Row]:
    """Pull every active shot in the lookback window with a non-NULL pHash.

    Soft-deleted rows (``deleted_at IS NOT NULL`` — see migration
    ``126_shot_deleted_at.sql``) are skipped: the operator already
    decided they are unwanted, we should not re-surface them as
    duplicates of an active row.
    """
    modifier = f"-{int(lookback_days)} days"
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, phash, app_name, thumbnail_path, tier "
            "FROM screenshots "
            "WHERE phash IS NOT NULL "
            "  AND deleted_at IS NULL "
            "  AND captured_at >= datetime(?, ?) "
            "ORDER BY captured_at ASC",
            ("now", modifier),
        )
        return list(await cursor.fetchall())


def _build_pairs(
    rows: list[aiosqlite.Row],
    *,
    threshold: int,
    uf: _UnionFind,
) -> int:
    """Pairwise-compare within a 1-hour sliding window; union matches.

    Returns the number of pairs that were actually compared so the
    caller can log it for back-pressure / perf debugging.
    """
    pair_count = 0
    timestamps = [_parse_iso_seconds(str(r["captured_at"])) for r in rows]

    window_start = 0
    for right_idx in range(len(rows)):
        right_ts = timestamps[right_idx]
        # Advance window_start until the window head is within
        # _WINDOW_SECONDS of the right edge. Both timestamps are
        # monotonically non-decreasing because the SQL ordered by
        # captured_at ASC, so this is O(N) amortised.
        while (
            window_start < right_idx
            and right_ts - timestamps[window_start] > _WINDOW_SECONDS
        ):
            window_start += 1
        right_phash = str(rows[right_idx]["phash"])
        right_id = int(rows[right_idx]["id"])
        for left_idx in range(window_start, right_idx):
            pair_count += 1
            left_phash = str(rows[left_idx]["phash"])
            try:
                distance = hamming_distance(left_phash, right_phash)
            except ValueError:
                # Mixed pHash lengths (legacy rows with a different
                # hash_size). One bad row should not break the scan.
                continue
            if distance <= threshold:
                uf.union(int(rows[left_idx]["id"]), right_id)
    return pair_count


def _pick_keep_id(group_rows: list[aiosqlite.Row]) -> int:
    """Return the row id we recommend keeping when the user bulk-deletes.

    Priority:

    1. Any row in the ``pinned`` tier (user explicitly marked it
       important).
    2. Oldest ``captured_at`` — the original capture, before the
       near-duplicate fork.

    ``group_rows`` is assumed to be pre-sorted by ``captured_at`` ASC
    by the caller.
    """
    for row in group_rows:
        tier = row["tier"] if "tier" in row.keys() else None  # noqa: SIM118
        if tier is not None and str(tier) == _PINNED_TIER:
            return int(row["id"])
    return int(group_rows[0]["id"])


def _parse_iso_seconds(timestamp: str) -> int:
    """Cheap ISO-8601 → epoch-seconds conversion for window comparisons.

    The scanner only needs *relative* ordering and a window predicate,
    so we avoid dragging in ``datetime.fromisoformat`` parsing for
    every row in a 10k-row scan and use a coarse string-prefix decode
    instead. Falls back to ``0`` for unparseable values — those rows
    end up in a permanent window-start position, which is a harmless
    over-approximation (they will be compared against more
    neighbours, not fewer).
    """
    # Format: "YYYY-MM-DDTHH:MM:SS[.fff][Z|+00:00]" or
    # "YYYY-MM-DD HH:MM:SS" (SQLite ``datetime('now')`` default).
    try:
        date_part = timestamp[:10]
        time_part = timestamp[11:19]
        year = int(date_part[0:4])
        month = int(date_part[5:7])
        day = int(date_part[8:10])
        hour = int(time_part[0:2])
        minute = int(time_part[3:5])
        second = int(time_part[6:8])
    except (ValueError, IndexError):
        return 0
    # Days-from-epoch via a simple proleptic Gregorian formula. We
    # don't care about absolute correctness — only that two
    # timestamps an hour apart produce values 3600 apart.
    days = (year - 1970) * 365 + (year - 1969) // 4 + month * 31 + day
    return days * 86400 + hour * 3600 + minute * 60 + second


async def soft_delete_shots(
    *,
    keep_id: int,
    delete_ids: list[int],
) -> int:
    """Soft-delete every id in ``delete_ids`` except ``keep_id``.

    Returns the number of rows actually updated. Uses parametrised
    SQL via :py:meth:`aiosqlite.Connection.executemany` so the id list
    never gets interpolated into the query string.

    "Soft" means stamping ``deleted_at`` — the row stays in the
    database (and on disk) so the operator can undo by clearing the
    column. Physical removal is the recycle-bin retention job's job,
    not this admin scanner's.
    """
    targets = [i for i in delete_ids if i != keep_id]
    if not targets:
        return 0
    async with get_connection() as conn:
        cursor = await conn.executemany(
            "UPDATE screenshots "
            "SET deleted_at = datetime('now') "
            "WHERE id = ? AND deleted_at IS NULL",
            [(shot_id,) for shot_id in targets],
        )
        affected = cursor.rowcount
        await conn.commit()
    log.info(
        "dup_finder.soft_delete",
        keep_id=keep_id,
        requested=len(targets),
        affected=affected,
    )
    return int(affected)
