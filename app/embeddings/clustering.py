"""Lightweight topic discovery via mini-batch k-means over stored embeddings.

We deliberately use a pure-Python implementation (no numpy required at
runtime — fastembed ships numpy but we don't want to make k-means depend
on it). For typical personal corpora (≤50k vectors × 384 dims) this is
fast enough that it doesn't justify a heavyweight library.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import aiosqlite

from app.embeddings.storage import decode_vector


@dataclass(slots=True)
class Cluster:
    centroid: list[float]
    member_ids: list[int]
    label: str  # auto-derived from top OCR tokens


async def discover_clusters(
    conn: aiosqlite.Connection,
    *,
    k: int = 8,
    max_vectors: int = 5000,
    iterations: int = 12,
) -> list[Cluster]:
    """Cluster the most recent `max_vectors` embeddings into `k` topics."""
    cursor = await conn.execute(
        """
        SELECT e.screenshot_id AS id, e.vector,
               s.window_title, s.app_name, s.ocr_text
        FROM screenshot_embeddings e
        JOIN screenshots s ON s.id = e.screenshot_id
        ORDER BY s.captured_at DESC
        LIMIT ?
        """,
        (max_vectors,),
    )
    rows = await cursor.fetchall()
    if len(rows) < k:
        return []

    items = [
        {
            "id": int(row["id"]),
            "vec": decode_vector(bytes(row["vector"])),
            "text": " ".join(
                filter(
                    None,
                    [
                        row["window_title"] or "",
                        (row["ocr_text"] or "")[:300],
                    ],
                )
            ),
        }
        for row in rows
    ]

    centroids = _kmeans(items, k=k, iterations=iterations)
    clusters: list[Cluster] = []
    for cent_idx, centroid in enumerate(centroids):
        members = [
            item for item in items if _nearest(item["vec"], centroids) == cent_idx
        ]
        if not members:
            continue
        clusters.append(
            Cluster(
                centroid=centroid,
                member_ids=[m["id"] for m in members],
                label=_label_from_texts([m["text"] for m in members]),
            )
        )
    clusters.sort(key=lambda c: len(c.member_ids), reverse=True)
    return clusters


def _kmeans(items: list[dict], *, k: int, iterations: int) -> list[list[float]]:
    if not items:
        return []

    rng = random.Random(42)
    seed_indexes = rng.sample(range(len(items)), k)
    centroids = [list(items[i]["vec"]) for i in seed_indexes]

    for _ in range(iterations):
        assignments = [_nearest(item["vec"], centroids) for item in items]
        new_centroids: list[list[float]] = [list(c) for c in centroids]
        for c_idx in range(k):
            members = [items[i]["vec"] for i, a in enumerate(assignments) if a == c_idx]
            if members:
                new_centroids[c_idx] = _mean(members)
        if _converged(centroids, new_centroids):
            return new_centroids
        centroids = new_centroids
    return centroids


def _nearest(vec: list[float], centroids: list[list[float]]) -> int:
    best = 0
    best_d = math.inf
    for i, c in enumerate(centroids):
        d = _distance_sq(vec, c)
        if d < best_d:
            best = i
            best_d = d
    return best


def _distance_sq(a: list[float], b: list[float]) -> float:
    return sum((x - y) * (x - y) for x, y in zip(a, b, strict=True))


def _mean(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    dims = len(vectors[0])
    out = [0.0] * dims
    for v in vectors:
        for i in range(dims):
            out[i] += v[i]
    return [x / n for x in out]


def _converged(a: list[list[float]], b: list[list[float]], eps: float = 1e-3) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b, strict=True):
        if len(x) != len(y):
            return False
        if _distance_sq(x, y) >= eps:
            return False
    return True


_STOPWORDS = frozenset(
    [
        "the", "a", "an", "and", "or", "of", "to", "for", "in", "on",
        "at", "by", "with", "is", "are", "be", "was", "this", "that",
        "it", "its", "as", "but", "from", "have", "has", "had",
        "и", "в", "на", "не", "что", "это", "как", "по", "к", "с",
        "из", "для", "от", "у", "о", "так", "же", "до", "за", "со",
        "при", "под", "над", "об", "то", "ли",
    ]
)


def _label_from_texts(texts: list[str]) -> str:
    """Heuristic 'topic name' — top 2 frequent non-stop tokens across cluster members."""
    freq: dict[str, int] = {}
    for text in texts:
        for token in text.lower().split():
            token = "".join(c for c in token if c.isalnum())
            if len(token) < 3 or token in _STOPWORDS:
                continue
            freq[token] = freq.get(token, 0) + 1
    if not freq:
        return "—"
    top = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return " · ".join(word for word, _ in top)
