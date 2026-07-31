"""Bounded, explicit pattern match for a research request — no model call.

See app/thinking/research_request.py: this runs on every incoming message,
so detection must stay a cheap regex, not an LLM turn.
"""
# ruff: noqa: RUF001

from __future__ import annotations

from app.thinking.research_request import build_research_request, detect_research_topic


def test_addressed_research_request_is_detected_with_its_topic() -> None:
    assert detect_research_topic("персик, посмотри лабиринт фавна") == "лабиринт фавна"


def test_ordinary_greeting_is_not_a_research_request() -> None:
    assert detect_research_topic("как дела?") is None


def test_various_trigger_verbs_are_recognised() -> None:
    assert detect_research_topic("почитай про Лабиринт Фавна") == "Лабиринт Фавна"
    assert detect_research_topic("узнай о новом айфоне") == "новом айфоне"
    assert detect_research_topic("глянь что за фильм Дюна") == "фильм Дюна"


def test_build_research_request_packages_chat_context() -> None:
    request = build_research_request(
        "персик, посмотри лабиринт фавна",
        chat_id=-100500,
        sender="Клод",
        source_scope="group",
    )
    assert request is not None
    assert request.topic == "лабиринт фавна"
    assert request.chat_id == -100500
    assert request.sender == "Клод"
    assert request.source_scope == "group"


def test_build_research_request_returns_none_without_a_trigger() -> None:
    assert (
        build_research_request(
            "как дела?", chat_id=-100500, sender="Клод", source_scope="group"
        )
        is None
    )


def test_empty_text_is_not_a_request() -> None:
    assert detect_research_topic("") is None
    assert detect_research_topic("   ") is None
