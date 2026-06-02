"""Perceptual-hash deduplication."""

from app.dedup.phash import (
    compute_phash,
    find_or_create_dedup_group,
    hamming_distance,
    is_near_duplicate,
)

__all__ = [
    "compute_phash",
    "find_or_create_dedup_group",
    "hamming_distance",
    "is_near_duplicate",
]
