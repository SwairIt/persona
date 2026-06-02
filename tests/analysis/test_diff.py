"""Tests for diff_screenshots."""

from __future__ import annotations

from app.analysis.diff import diff_screenshots


def test_diff_identical_text() -> None:
    result = diff_screenshots(left_ocr="hello world", right_ocr="hello world")
    assert result.added == []
    assert result.removed == []
    assert result.unchanged_ratio == 1.0


def test_diff_added_words() -> None:
    result = diff_screenshots(left_ocr="hello", right_ocr="hello world")
    assert "world" in result.added
    assert result.removed == []


def test_diff_removed_words() -> None:
    result = diff_screenshots(left_ocr="hello world today", right_ocr="hello today")
    assert "world" in result.removed
    assert result.added == []


def test_diff_both_added_and_removed() -> None:
    result = diff_screenshots(
        left_ocr="meeting at 14:00 with Anna",
        right_ocr="meeting at 16:00 with Boris",
    )
    assert "14:00" in result.removed
    assert "Anna" in result.removed
    assert "16:00" in result.added
    assert "Boris" in result.added


def test_diff_empty_inputs() -> None:
    result = diff_screenshots(left_ocr=None, right_ocr=None)
    assert result.added == []
    assert result.removed == []


def test_diff_phash_distance() -> None:
    result = diff_screenshots(
        left_ocr="x",
        right_ocr="x",
        left_phash="ffff",
        right_phash="fffe",
    )
    assert result.phash_hamming == 1


def test_diff_phash_mismatched_lengths_safe() -> None:
    result = diff_screenshots(
        left_ocr="x",
        right_ocr="x",
        left_phash="ff",
        right_phash="ffff",
    )
    assert result.phash_hamming is None
