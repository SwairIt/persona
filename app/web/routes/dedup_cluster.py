"""Admin UI for inspecting + reshaping dedup clusters (v0.97 feature 3/3).

A "dedup cluster" is a row in :pyref:`dedup_groups` together with the
``screenshots`` rows whose ``dedup_group_id`` points at it. The detector
in :mod:`app.dedup.phash` builds them automatically by perceptual-hash
similarity, but the result is occasionally wrong — two visually distinct
frames can collide under pHash, or one logical scene can fragment into
two groups across a long session. This page is the operator's escape
hatch:

* ``GET  /admin/dedup-clusters?page=N`` renders one page of clusters,
  ordered by largest cluster first, with member thumbnails + total
  on-disk size for each.
* ``POST /admin/dedup-clusters/{group_id}/split`` "explodes" a cluster
  by clearing ``dedup_group_id`` on every non-anchor member, leaving the
  representative shot alone in the group. Use when pHash over-merged
  unrelated frames.
* ``POST /admin/dedup-clusters/{group_id}/merge`` folds this cluster
  *into* a target cluster (``target_id`` form field) by repointing every
  member's ``dedup_group_id`` and then deleting the now-empty source
  row. Use when one logical scene fragmented into two groups.

Design contract
---------------
* **Parametrised SQL only.** Every value passed to SQLite travels as a
  ``?`` placeholder; nothing user-supplied is interpolated into a query
  string. Pagination clamps page+size to defensive bounds so a typo
  cannot materialise a million-row scan.
* **Audit-logged.** Every state-changing endpoint emits exactly one
  ``log_action`` row (success or failure) with the affected group id +
  enough metadata to reconstruct what happened during an incident
  review — never any OCR text or other potentially sensitive payload.
* **Best-effort disk sizing.** Member size on disk is the sum of
  ``thumbnail_path.stat().st_size`` for each member with a thumbnail
  present; a missing or unreadable file contributes ``0`` rather than
  500-ing the whole page. The figure is informational, not transactional.
* **No-op safety.** Split on a 1-member cluster, or merge into self, is
  refused with a 400 before touching the DB so the audit log stays
  meaningful.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

router = APIRouter(tags=["dedup-cluster"])
log = get_logger("persona.dedup_cluster")

# Page size for the cluster listing. Each card carries up to
# ``_THUMBS_PER_CARD`` thumbnail <img> tags so the rendered DOM stays
# reasonable on a slow laptop — 20 cards * 12 thumbs = 240 images, well
# under what modern browsers handle smoothly with ``loading="lazy"``.
_PAGE_SIZE = 20

# Hard ceiling on ``?page=`` so an arithmetic typo (``?page=10**9``)
# cannot push SQLite into a giant OFFSET scan. Way above any real
# operator-visible value.
_MAX_PAGE = 10_000

# Per-card thumbnail cap. Clusters with more than this many members
# render the first ``_THUMBS_PER_CARD`` thumbs and a "+N more" hint;
# the count + size figures still reflect the *whole* cluster.
_THUMBS_PER_CARD = 12


def _safe_disk_size(thumbnail_path: str | None) -> int:
    """Return ``thumbnail_path.stat().st_size`` or ``0`` on any error.

    Missing files, permission errors, or rows with ``NULL`` thumbnails
    all contribute zero rather than aborting the size tally for the
    whole cluster. The figure shown on the page is informational; we
    deliberately never raise from this helper.
    """
    if not thumbnail_path:
        return 0
    try:
        return Path(thumbnail_path).stat().st_size
    except OSError:
        return 0


async def _count_clusters(conn: aiosqlite.Connection) -> int:
    """Return the total number of dedup_group rows in the DB."""
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM dedup_groups")
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _fetch_page(
    conn: aiosqlite.Connection,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Return one page of dedup_groups, largest cluster first.

    "Largest" is decided by the live ``COUNT(screenshots.id)`` rather
    than the cached ``seen_count`` column — the cached value can drift
    after manual splits/merges, and the operator's whole reason for
    visiting this page is usually that the cached numbers are no longer
    trustworthy.
    """
    cursor = await conn.execute(
        "SELECT g.id AS group_id, "
        "       g.representative_screenshot_id AS rep_id, "
        "       g.phash AS phash, "
        "       g.first_seen AS first_seen, "
        "       g.last_seen AS last_seen, "
        "       COUNT(s.id) AS member_count "
        "FROM dedup_groups g "
        "LEFT JOIN screenshots s ON s.dedup_group_id = g.id "
        "GROUP BY g.id "
        "ORDER BY member_count DESC, g.id DESC "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    return [
        {
            "group_id": int(row["group_id"]),
            "rep_id": (None if row["rep_id"] is None else int(row["rep_id"])),
            "phash": str(row["phash"]),
            "first_seen": str(row["first_seen"]),
            "last_seen": str(row["last_seen"]),
            "member_count": int(row["member_count"]),
        }
        for row in rows
    ]


async def _fetch_members(
    conn: aiosqlite.Connection,
    group_id: int,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return the first ``limit`` member shots for ``group_id``.

    Ordered by ``captured_at ASC`` so the visual narrative reads
    left-to-right in chronological order — the operator can usually
    eyeball "yep, these all look the same" or "wait, frame 7 is a
    different window" from that progression alone.
    """
    cursor = await conn.execute(
        "SELECT id, captured_at, thumbnail_path, app_name, window_title "
        "FROM screenshots "
        "WHERE dedup_group_id = ? "
        "ORDER BY captured_at ASC "
        "LIMIT ?",
        (group_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "thumbnail_path": (
                None if row["thumbnail_path"] is None else str(row["thumbnail_path"])
            ),
            "app_name": (None if row["app_name"] is None else str(row["app_name"])),
            "window_title": (None if row["window_title"] is None else str(row["window_title"])),
        }
        for row in rows
    ]


async def _sum_cluster_bytes(conn: aiosqlite.Connection, group_id: int) -> int:
    """Sum on-disk thumbnail size across every member of ``group_id``.

    Walks all members (not just the visible thumbnails on the card) so
    the displayed "X.X MB" reflects the whole cluster's footprint, which
    is what the operator actually cares about when deciding whether a
    split is worth doing.
    """
    cursor = await conn.execute(
        "SELECT thumbnail_path FROM screenshots WHERE dedup_group_id = ?",
        (group_id,),
    )
    rows = await cursor.fetchall()
    total = 0
    for row in rows:
        total += _safe_disk_size(
            None if row["thumbnail_path"] is None else str(row["thumbnail_path"])
        )
    return total


@router.get("/admin/dedup-clusters", response_class=HTMLResponse)
async def dedup_clusters_page(request: Request, page: int = 1) -> HTMLResponse:
    """Render one page of dedup clusters with member previews."""
    # Clamp page into a sane window before any DB work. Negative or
    # absurd values silently collapse to ``1`` / ``_MAX_PAGE`` so a
    # broken bookmark cannot DoS the server.
    safe_page = max(1, min(int(page), _MAX_PAGE))
    offset = (safe_page - 1) * _PAGE_SIZE

    async with get_connection() as conn:
        total_clusters = await _count_clusters(conn)
        clusters = await _fetch_page(conn, limit=_PAGE_SIZE, offset=offset)
        for cluster in clusters:
            cluster["members"] = await _fetch_members(
                conn, cluster["group_id"], limit=_THUMBS_PER_CARD
            )
            cluster["size_bytes"] = await _sum_cluster_bytes(conn, cluster["group_id"])
            cluster["hidden_member_count"] = max(
                0, cluster["member_count"] - len(cluster["members"])
            )

    total_pages = max(1, (total_clusters + _PAGE_SIZE - 1) // _PAGE_SIZE)
    has_prev = safe_page > 1
    has_next = safe_page < total_pages

    log.info(
        "dedup_cluster.page",
        page=safe_page,
        rendered=len(clusters),
        total=total_clusters,
    )

    return templates.TemplateResponse(
        request,
        "dedup_cluster.html",
        {
            "title": "Dedup clusters",
            "active_nav": "settings",
            "clusters": clusters,
            "page": safe_page,
            "page_size": _PAGE_SIZE,
            "total_clusters": total_clusters,
            "total_pages": total_pages,
            "has_prev": has_prev,
            "has_next": has_next,
        },
    )


async def _load_group(conn: aiosqlite.Connection, group_id: int) -> dict[str, Any] | None:
    """Return ``{rep_id, member_count}`` for one cluster, or ``None``."""
    cursor = await conn.execute(
        "SELECT g.representative_screenshot_id AS rep_id, "
        "       COUNT(s.id) AS member_count "
        "FROM dedup_groups g "
        "LEFT JOIN screenshots s ON s.dedup_group_id = g.id "
        "WHERE g.id = ? "
        "GROUP BY g.id",
        (group_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "rep_id": None if row["rep_id"] is None else int(row["rep_id"]),
        "member_count": int(row["member_count"]),
    }


@router.post("/admin/dedup-clusters/{group_id}/split")
async def dedup_cluster_split(group_id: int) -> RedirectResponse:
    """Clear ``dedup_group_id`` on every non-anchor member of ``group_id``.

    The anchor (``representative_screenshot_id``) stays attached so the
    group keeps a single member rather than going empty — that way the
    pHash detector's "find existing group for this hash" lookup still
    works for *future* frames that match the canonical representative,
    while the ones we just exploded are free to land in fresh groups of
    their own on the next capture pass.

    Refuses with 400 on a group that has 0 or 1 members (nothing to
    split) so the audit log doesn't fill up with no-op rows.
    """
    async with get_connection() as conn:
        info = await _load_group(conn, group_id)
        if info is None:
            await log_action(
                "dedup_cluster.split",
                target=str(group_id),
                detail="group not found",
                success=False,
            )
            raise HTTPException(status_code=404, detail="Dedup group not found")

        if info["member_count"] <= 1:
            await log_action(
                "dedup_cluster.split",
                target=str(group_id),
                detail=f"nothing to split (members={info['member_count']})",
                success=False,
            )
            raise HTTPException(
                status_code=400,
                detail="Cluster has nothing to split (need at least 2 members)",
            )

        rep_id = info["rep_id"]
        # If the cluster lacks a representative for any reason (legacy
        # row written before the detector started populating it), fall
        # back to "keep the oldest member as anchor" so the split still
        # leaves the group with exactly one member.
        if rep_id is None:
            cursor = await conn.execute(
                "SELECT id FROM screenshots "
                "WHERE dedup_group_id = ? "
                "ORDER BY captured_at ASC "
                "LIMIT 1",
                (group_id,),
            )
            anchor_row = await cursor.fetchone()
            rep_id = int(anchor_row["id"]) if anchor_row is not None else None

        # If even the fallback returns nothing (member_count > 1 but
        # zero rows came back — should not happen, but defensive) bail
        # out rather than emit an unscoped UPDATE that detaches every
        # row in the cluster including a future anchor.
        if rep_id is None:  # pragma: no cover — defensive
            await log_action(
                "dedup_cluster.split",
                target=str(group_id),
                detail="no anchor candidate found",
                success=False,
            )
            raise HTTPException(status_code=500, detail="Cluster has no usable anchor")

        cursor = await conn.execute(
            "UPDATE screenshots SET dedup_group_id = NULL WHERE dedup_group_id = ? AND id != ?",
            (group_id, rep_id),
        )
        detached = cursor.rowcount or 0
        # Refresh the cached counter so the listing's seen_count column
        # doesn't lie about how many members are still attached.
        await conn.execute(
            "UPDATE dedup_groups SET seen_count = 1 WHERE id = ?",
            (group_id,),
        )
        await conn.commit()

    log.info(
        "dedup_cluster.split",
        group_id=group_id,
        anchor_id=rep_id,
        detached=detached,
    )
    await log_action(
        "dedup_cluster.split",
        target=str(group_id),
        detail=f"anchor={rep_id} detached={detached}",
    )
    return RedirectResponse(url="/admin/dedup-clusters", status_code=303)


@router.post("/admin/dedup-clusters/{group_id}/merge")
async def dedup_cluster_merge(group_id: int, target_id: int = Form(...)) -> RedirectResponse:
    """Fold ``group_id`` into ``target_id`` and delete the now-empty source.

    Every member's ``dedup_group_id`` is repointed at ``target_id`` in
    one UPDATE, then ``dedup_groups.seen_count`` on the destination is
    refreshed from the live COUNT, and finally the source row is deleted
    so the listing doesn't sprout phantom empty clusters.

    Refuses self-merges and missing targets with 400 / 404 before
    touching the DB so the audit log stays meaningful.
    """
    if group_id == target_id:
        await log_action(
            "dedup_cluster.merge",
            target=f"{group_id}->{target_id}",
            detail="source equals target",
            success=False,
        )
        raise HTTPException(status_code=400, detail="Source and target must differ")

    async with get_connection() as conn:
        source = await _load_group(conn, group_id)
        target = await _load_group(conn, target_id)
        if source is None or target is None:
            await log_action(
                "dedup_cluster.merge",
                target=f"{group_id}->{target_id}",
                detail=(
                    f"missing group (source_present={source is not None} "
                    f"target_present={target is not None})"
                ),
                success=False,
            )
            raise HTTPException(status_code=404, detail="Dedup group not found")

        cursor = await conn.execute(
            "UPDATE screenshots SET dedup_group_id = ? WHERE dedup_group_id = ?",
            (target_id, group_id),
        )
        moved = cursor.rowcount or 0

        # Refresh the destination's cached counter from the live count
        # so subsequent listings show the merged total without waiting
        # for the next capture pass to bump it.
        count_cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots WHERE dedup_group_id = ?",
            (target_id,),
        )
        count_row = await count_cursor.fetchone()
        new_total = int(count_row["n"]) if count_row is not None else 0
        await conn.execute(
            "UPDATE dedup_groups SET seen_count = ? WHERE id = ?",
            (new_total, target_id),
        )

        # The source is now guaranteed empty (its only members just got
        # repointed). Delete it so the listing doesn't accumulate empty
        # clusters every time an operator runs a merge.
        await conn.execute(
            "DELETE FROM dedup_groups WHERE id = ?",
            (group_id,),
        )
        await conn.commit()

    log.info(
        "dedup_cluster.merge",
        source_id=group_id,
        target_id=target_id,
        moved=moved,
        target_total=new_total,
    )
    await log_action(
        "dedup_cluster.merge",
        target=f"{group_id}->{target_id}",
        detail=f"moved={moved} target_total={new_total}",
    )
    return RedirectResponse(url="/admin/dedup-clusters", status_code=303)


# Re-export the names ``mypy --strict`` insists on for module-level publics.
_PAGE_SIZE_PUBLIC: int = _PAGE_SIZE
_THUMBS_PER_CARD_PUBLIC: int = _THUMBS_PER_CARD


__all__: Sequence[str] = (
    "dedup_cluster_merge",
    "dedup_cluster_split",
    "dedup_clusters_page",
    "router",
)
