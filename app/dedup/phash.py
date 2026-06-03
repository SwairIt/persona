"""pHash-based near-duplicate detection."""

from __future__ import annotations

from datetime import datetime

import aiosqlite
import imagehash
from PIL import Image

from app.storage.repository import (
    bump_dedup_group,
    find_dedup_group_by_phash,
    insert_dedup_group,
    list_recent_dedup_groups,
)
from app.storage_savings import record_dedup_hit

# Estimated bytes the would-be screenshot+thumbnail would have occupied
# on disk if the dedup pass had not skipped it. The dedup decision runs
# pre-write so there is no real file to measure — we credit a single
# fixed JPEG-sized estimate per hit, deliberately conservative, so the
# savings chart reports a believable lower bound rather than an
# optimistic guess that drifts with capture resolution.
_DEDUP_HIT_BYTES_ESTIMATE = 50 * 1024


def compute_phash(image: Image.Image, *, hash_size: int = 8) -> str:
    """Return a hex perceptual hash of the given image (default 64-bit)."""
    return str(imagehash.phash(image, hash_size=hash_size))


def hamming_distance(left: str, right: str) -> int:
    """Hamming distance between two hex pHash strings."""
    if len(left) != len(right):
        msg = f"phash length mismatch: {len(left)} vs {len(right)}"
        raise ValueError(msg)
    left_int = int(left, 16)
    right_int = int(right, 16)
    return (left_int ^ right_int).bit_count()


def is_near_duplicate(left: str, right: str, *, threshold: int) -> bool:
    """True if the two pHashes are within the hamming threshold."""
    return hamming_distance(left, right) <= threshold


async def find_or_create_dedup_group(
    conn: aiosqlite.Connection,
    *,
    phash: str,
    now: datetime,
    threshold: int,
    candidate_limit: int = 200,
) -> tuple[int, bool]:
    """Return (group_id, is_new). Matches by exact pHash first, then near.

    "is_new" is True when a new group was just inserted.
    """
    exact = await find_dedup_group_by_phash(conn, phash)
    if exact is not None:
        await bump_dedup_group(conn, exact.id, last_seen=now)
        await record_dedup_hit(_DEDUP_HIT_BYTES_ESTIMATE)
        return exact.id, False

    candidates = await list_recent_dedup_groups(conn, limit=candidate_limit)
    for group in candidates:
        try:
            if is_near_duplicate(phash, group.phash, threshold=threshold):
                await bump_dedup_group(conn, group.id, last_seen=now)
                await record_dedup_hit(_DEDUP_HIT_BYTES_ESTIMATE)
                return group.id, False
        except ValueError:
            continue

    new_id = await insert_dedup_group(
        conn,
        phash=phash,
        representative_screenshot_id=None,
        first_seen=now,
    )
    return new_id, True
