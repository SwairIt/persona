"""Tests for the pure-Python k-means clustering helper."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from app.embeddings.clustering import (
    _converged,
    _distance_sq,
    _kmeans,
    _label_from_texts,
    _mean,
    _nearest,
    discover_clusters,
)
from app.embeddings.storage import upsert_embedding
from app.storage.repository import insert_screenshot, update_screenshot_ocr


def test_distance_sq_zero_for_identical() -> None:
    assert _distance_sq([1.0, 2.0], [1.0, 2.0]) == 0.0


def test_mean_centroid() -> None:
    assert _mean([[1.0, 1.0], [3.0, 3.0]]) == [2.0, 2.0]


def test_nearest_picks_closest() -> None:
    centroids = [[0.0, 0.0], [10.0, 10.0]]
    assert _nearest([0.1, 0.1], centroids) == 0
    assert _nearest([9.9, 9.9], centroids) == 1


def test_converged_when_centroids_unchanged() -> None:
    assert _converged([[1.0, 2.0]], [[1.0, 2.0]]) is True
    assert _converged([[1.0]], [[1.0, 2.0]]) is False


def test_kmeans_separates_two_groups() -> None:
    items = [
        {"vec": [0.0, 0.0]},
        {"vec": [0.1, 0.1]},
        {"vec": [10.0, 10.0]},
        {"vec": [10.1, 10.1]},
    ]
    centroids = _kmeans(items, k=2, iterations=20)
    assert len(centroids) == 2
    sums = sorted(sum(c) for c in centroids)
    assert sums[0] < 1.0
    assert sums[1] > 19.0


def test_label_picks_top_tokens() -> None:
    label = _label_from_texts(
        [
            "auth migration sqlalchemy",
            "auth migration sqlalchemy bug",
            "code review the auth flow",
        ]
    )
    assert "auth" in label
    assert "migration" in label or "sqlalchemy" in label


@pytest.mark.asyncio
async def test_discover_clusters_returns_empty_when_too_few(
    db: aiosqlite.Connection,
) -> None:
    clusters = await discover_clusters(db, k=4)
    assert clusters == []


@pytest.mark.asyncio
async def test_discover_clusters_returns_groups(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    group_a_vecs = [[0.0, 0.0], [0.1, 0.1], [0.2, 0.0]]
    group_b_vecs = [[10.0, 10.0], [10.2, 10.0], [9.8, 10.3]]

    for i, vec in enumerate(group_a_vecs + group_b_vecs):
        sid = await insert_screenshot(
            db,
            captured_at=now,
            width=1,
            height=1,
            phash=f"clust{i:013d}",
            app_name="App",
            window_title=f"Title {i}",
        )
        await update_screenshot_ocr(db, sid, ocr_text=f"text auth {i}", ocr_status="done")
        await upsert_embedding(db, screenshot_id=sid, vector=vec, model="t", text=f"text auth {i}")

    clusters = await discover_clusters(db, k=2, iterations=20)
    assert len(clusters) == 2
    assert sorted(len(c.member_ids) for c in clusters) == [3, 3]
