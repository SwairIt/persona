"""Tests for embeddings storage and search-mode wiring — no fastembed needed."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from app.embeddings.search import _cosine, _make_snippet
from app.embeddings.storage import (
    count_embeddings,
    decode_vector,
    encode_vector,
    fetch_embedding,
    list_unindexed_screenshots,
    text_fingerprint,
    upsert_embedding,
)
from app.storage.repository import insert_screenshot, update_screenshot_ocr


def test_encode_decode_roundtrip() -> None:
    original = [0.0, 1.0, -1.0, 0.5, -0.25, 3.14159]
    blob = encode_vector(original)
    decoded = decode_vector(blob)
    assert len(decoded) == len(original)
    for src, dst in zip(original, decoded, strict=True):
        assert abs(src - dst) < 1e-5


def test_text_fingerprint_is_stable_and_short() -> None:
    fp = text_fingerprint("hello persona")
    assert len(fp) == 16
    assert fp == text_fingerprint("hello persona")


def test_cosine_identical_is_one() -> None:
    v = [1.0, 2.0, 3.0]
    assert abs(_cosine(v, v) - 1.0) < 1e-6


def test_cosine_orthogonal_is_zero() -> None:
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_empty_safe() -> None:
    assert _cosine([], []) == 0.0
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_make_snippet_finds_window() -> None:
    text = "first second third fourth fifth target sixth seventh eighth"
    snippet = _make_snippet(text, "target")
    assert "target" in snippet


def test_make_snippet_no_match_returns_prefix() -> None:
    text = "lorem ipsum dolor sit amet"
    snippet = _make_snippet(text, "absent", max_len=12)
    assert snippet.startswith("lorem")


@pytest.mark.asyncio
async def test_upsert_and_fetch(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="e1e1e1e1e1e1e1e1",
        ocr_status="done",
    )
    await update_screenshot_ocr(db, sid, ocr_text="hello persona", ocr_status="done")
    await upsert_embedding(
        db,
        screenshot_id=sid,
        vector=[0.1, 0.2, 0.3, 0.4],
        model="test-model",
        text="hello persona",
    )
    rec = await fetch_embedding(db, sid)
    assert rec is not None
    assert rec["dim"] == 4
    assert rec["model"] == "test-model"
    assert len(rec["vector"]) == 4
    for src, dst in zip([0.1, 0.2, 0.3, 0.4], rec["vector"], strict=True):
        assert abs(src - dst) < 1e-5


@pytest.mark.asyncio
async def test_list_unindexed(db: aiosqlite.Connection) -> None:
    short_id = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="0001"
    )
    await update_screenshot_ocr(db, short_id, ocr_text="hi", ocr_status="done")

    long_id = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="0002"
    )
    await update_screenshot_ocr(
        db, long_id, ocr_text="this text is definitely long enough to be indexed", ocr_status="done"
    )

    pending = await list_unindexed_screenshots(db, min_text_length=20, limit=10)
    assert [p["id"] for p in pending] == [long_id]


@pytest.mark.asyncio
async def test_count_embeddings(db: aiosqlite.Connection) -> None:
    assert await count_embeddings(db) == 0
    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="0003"
    )
    await upsert_embedding(db, screenshot_id=sid, vector=[0.0, 1.0], model="m", text="x")
    assert await count_embeddings(db) == 1


@pytest.mark.asyncio
async def test_reindex_on_model_change(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="0004"
    )
    await update_screenshot_ocr(
        db, sid, ocr_text="text long enough to satisfy the minimum cutoff", ocr_status="done"
    )
    await upsert_embedding(db, screenshot_id=sid, vector=[0.1, 0.2], model="old-model", text="x")

    pending_same = await list_unindexed_screenshots(db, min_text_length=10, model="old-model")
    assert pending_same == []

    pending_new = await list_unindexed_screenshots(db, min_text_length=10, model="new-model")
    assert any(item["id"] == sid for item in pending_new)
