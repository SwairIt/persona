"""Tests for perceptual-hash deduplication."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest
from PIL import Image

from app.dedup.phash import (
    compute_phash,
    find_or_create_dedup_group,
    hamming_distance,
    is_near_duplicate,
)


def _make_image(colour: tuple[int, int, int], size: tuple[int, int] = (256, 256)) -> Image.Image:
    return Image.new("RGB", size, colour)


def test_compute_phash_is_stable_for_identical_images() -> None:
    img_a = _make_image((128, 64, 200))
    img_b = _make_image((128, 64, 200))
    assert compute_phash(img_a) == compute_phash(img_b)


def test_compute_phash_differs_for_different_images() -> None:
    img_a = _make_image((255, 0, 0))
    img_b = _make_image((0, 255, 0))
    assert compute_phash(img_a) != compute_phash(img_b)


def test_hamming_distance_basic() -> None:
    assert hamming_distance("0000", "0000") == 0
    assert hamming_distance("0000", "ffff") == 16
    assert hamming_distance("1234", "1234") == 0


def test_hamming_distance_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        hamming_distance("ff", "ffff")


def test_is_near_duplicate_threshold() -> None:
    assert is_near_duplicate("ffff", "ffff", threshold=0) is True
    assert is_near_duplicate("ffff", "fffe", threshold=1) is True
    assert is_near_duplicate("ffff", "fff0", threshold=3) is False


@pytest.mark.asyncio
async def test_find_or_create_dedup_group_exact_match(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    gid1, new1 = await find_or_create_dedup_group(db, phash="abcdabcdabcdabcd", now=now, threshold=4)
    gid2, new2 = await find_or_create_dedup_group(db, phash="abcdabcdabcdabcd", now=now, threshold=4)
    assert new1 is True
    assert new2 is False
    assert gid1 == gid2


@pytest.mark.asyncio
async def test_find_or_create_dedup_group_near_match(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    gid1, _ = await find_or_create_dedup_group(db, phash="ffff000011112222", now=now, threshold=4)
    gid2, new2 = await find_or_create_dedup_group(db, phash="ffff000011112223", now=now, threshold=4)
    assert new2 is False
    assert gid1 == gid2


@pytest.mark.asyncio
async def test_find_or_create_dedup_group_distinct_far(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    gid1, _ = await find_or_create_dedup_group(db, phash="0000000000000000", now=now, threshold=2)
    gid2, new2 = await find_or_create_dedup_group(db, phash="ffffffffffffffff", now=now, threshold=2)
    assert new2 is True
    assert gid1 != gid2
