"""Tests for thumbnail writer."""

from __future__ import annotations

from datetime import datetime, timezone

from PIL import Image

from app.storage.thumbnails import (
    delete_thumbnail,
    list_dated_subfolders,
    save_thumbnail,
)


def test_save_thumbnail_writes_webp() -> None:
    image = Image.new("RGB", (2560, 1440), (50, 60, 70))
    captured_at = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
    path = save_thumbnail(image, captured_at, screenshot_id=42)
    assert path.exists()
    assert path.suffix == ".webp"
    assert path.name == "42.webp"
    assert "2026-06-02" in str(path)


def test_save_thumbnail_resizes_wide_images() -> None:
    image = Image.new("RGB", (3840, 2160), (0, 0, 0))
    captured_at = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
    path = save_thumbnail(image, captured_at, screenshot_id=1, max_width=1280)
    with Image.open(path) as written:
        assert written.width == 1280
        assert written.height == 720


def test_save_thumbnail_skips_resize_when_already_small() -> None:
    image = Image.new("RGB", (800, 600), (10, 20, 30))
    captured_at = datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)
    path = save_thumbnail(image, captured_at, screenshot_id=2, max_width=1280)
    with Image.open(path) as written:
        assert written.width == 800
        assert written.height == 600


def test_delete_thumbnail_returns_false_for_missing() -> None:
    assert delete_thumbnail(None) is False


def test_list_dated_subfolders_skips_non_date_names() -> None:
    image = Image.new("RGB", (64, 64), (1, 2, 3))
    save_thumbnail(image, datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc), screenshot_id=11)
    save_thumbnail(image, datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc), screenshot_id=12)
    folders = list_dated_subfolders()
    names = [p.name for p in folders]
    assert "2026-06-02" in names
    assert "2026-06-03" in names
