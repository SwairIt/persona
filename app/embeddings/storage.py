"""SQLite BLOB persistence for float32 embeddings."""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import aiosqlite


def encode_vector(values: list[float]) -> bytes:
    """Pack a list of floats into a contiguous little-endian float32 BLOB."""
    return struct.pack(f"<{len(values)}f", *values)


def decode_vector(blob: bytes) -> list[float]:
    """Unpack a float32 BLOB back into a Python list."""
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def text_fingerprint(text: str) -> str:
    """Stable short hash of the indexed text — lets us skip re-embedding."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]  # noqa: S324


async def upsert_embedding(
    conn: aiosqlite.Connection,
    *,
    screenshot_id: int,
    vector: list[float],
    model: str,
    text: str,
) -> None:
    blob = encode_vector(vector)
    await conn.execute(
        """
        INSERT INTO screenshot_embeddings (screenshot_id, model, dim, vector, text_hash)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(screenshot_id) DO UPDATE SET
            model = excluded.model,
            dim = excluded.dim,
            vector = excluded.vector,
            text_hash = excluded.text_hash,
            created_at = datetime('now')
        """,
        (screenshot_id, model, len(vector), blob, text_fingerprint(text)),
    )
    await conn.commit()


async def fetch_embedding(
    conn: aiosqlite.Connection,
    screenshot_id: int,
) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "SELECT screenshot_id, model, dim, vector, text_hash, created_at "
        "FROM screenshot_embeddings WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "screenshot_id": int(row["screenshot_id"]),
        "model": str(row["model"]),
        "dim": int(row["dim"]),
        "vector": decode_vector(bytes(row["vector"])),
        "text_hash": str(row["text_hash"]),
    }


async def list_unindexed_screenshots(
    conn: aiosqlite.Connection,
    *,
    min_text_length: int = 20,
    limit: int = 32,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Find screenshots with OCR text but no embedding for the current model.

    Includes rows whose stored embedding was produced by a different model
    (so changing PERSONA_EMBEDDINGS_MODEL re-indexes automatically).
    """
    params: list[Any] = [min_text_length, limit]
    model_clause = ""
    if model is not None:
        model_clause = " OR e.model != ?"
        params.insert(0, model)  # but we need correct ordering — use positional below

    if model is not None:
        sql = (
            "SELECT s.id, s.ocr_text FROM screenshots s "
            "LEFT JOIN screenshot_embeddings e ON e.screenshot_id = s.id "
            "WHERE s.ocr_status = 'done' "
            "  AND s.ocr_text IS NOT NULL "
            "  AND length(s.ocr_text) >= ? "
            "  AND (e.screenshot_id IS NULL OR e.model != ?) "
            "ORDER BY s.captured_at DESC LIMIT ?"
        )
        ordered: list[Any] = [min_text_length, model, limit]
    else:
        sql = (
            "SELECT s.id, s.ocr_text FROM screenshots s "
            "LEFT JOIN screenshot_embeddings e ON e.screenshot_id = s.id "
            "WHERE s.ocr_status = 'done' "
            "  AND s.ocr_text IS NOT NULL "
            "  AND length(s.ocr_text) >= ? "
            "  AND e.screenshot_id IS NULL "
            "ORDER BY s.captured_at DESC LIMIT ?"
        )
        ordered = [min_text_length, limit]

    cursor = await conn.execute(sql, ordered)
    rows = await cursor.fetchall()
    return [{"id": int(row["id"]), "text": str(row["ocr_text"])} for row in rows]


async def count_embeddings(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshot_embeddings")
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0
