"""Tests for the auto-tag LLM response parser."""

from __future__ import annotations

from app.llm.auto_tag import _parse_tags


def test_parses_clean_json() -> None:
    text = '{"tags": ["auth", "migration", "sqlalchemy"]}'
    tags = _parse_tags(text)
    assert tags == ["auth", "migration", "sqlalchemy"]


def test_parses_json_with_preamble() -> None:
    text = 'Sure, here you go:\n{"tags":["meeting","anna","monday"]}\nGood luck!'
    tags = _parse_tags(text)
    assert "meeting" in tags
    assert "anna" in tags
    assert "monday" in tags


def test_lowercases_and_kebabs_multiword() -> None:
    text = '{"tags": ["Auth Bug", "Code Review", "Slack DM"]}'
    tags = _parse_tags(text)
    assert "auth-bug" in tags
    assert "code-review" in tags


def test_filters_too_short_or_long() -> None:
    text = '{"tags": ["a", "ok", "fine", "' + "x" * 50 + '"]}'
    tags = _parse_tags(text)
    assert "a" not in tags
    assert "ok" in tags
    assert "fine" in tags
    assert not any(len(t) > 32 for t in tags)


def test_dedupes() -> None:
    text = '{"tags": ["auth", "auth", "AUTH"]}'
    tags = _parse_tags(text)
    assert tags.count("auth") == 1


def test_empty_input() -> None:
    assert _parse_tags("") == []
    assert _parse_tags("nothing here") == []


def test_non_json_object_returns_empty() -> None:
    assert _parse_tags('["auth", "meeting"]') == []


def test_handles_cyrillic_tags() -> None:
    text = '{"tags": ["встреча", "анна", "проект"]}'
    tags = _parse_tags(text)
    assert "встреча" in tags
    assert "анна" in tags
    assert "проект" in tags
