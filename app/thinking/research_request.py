"""Detect a research request addressed to Persona in ordinary chat text.

The owner's scenario: someone in a Telegram group tells Persona to look
something up ("персик, посмотри Лабиринт Фавна"). This module recognises
that shape of sentence and turns it into a :class:`ResearchRequest` — the
topic, which chat it came from, who asked, and that chat's privacy scope —
without calling a model. Detection runs on every incoming message, so it
must stay a bounded, explicit pattern match: no LLM call, no unbounded
regex backtracking, no network access.

This module makes no claim about *whether* a chat is allowed to trigger
research (that is a caller decision, e.g. checking the chat is in
``telegram_allowed_chat_ids``) and does not decide *how* the request is
answered — it only recognises the sentence and packages what it found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Explicit, bounded set of trigger verbs the owner described. Each may carry
# a short imperative suffix ("посмотри"/"посмотрите", "почитай"/"почитайте",
# etc.) — captured with a small closed alternation, not open-ended ``\w*``,
# so the pattern can never run away on adversarial input.
_TRIGGER = (
    r"(?:посмотри|посмотрите|глянь|гляньте|почитай|почитайте|"
    r"изучи|изучите|узнай|узнайте|разузнай|разузнайте)"
)

# An optional short preposition before the topic ("посмотри что за X",
# "почитай про X", "узнай о X"). Optional because "посмотри X" alone is
# also a valid request.
_PREPOSITION = r"(?:что\s+за|про|насчёт|об|о)\s+"

_REQUEST_RE = re.compile(
    rf"{_TRIGGER}\s+(?:{_PREPOSITION})?(?P<topic>.+)",
    re.IGNORECASE,
)

_MAX_TOPIC_CHARS = 300


def _clean_topic(raw: str) -> str:
    topic = raw.strip().strip(" .,!?:;\"'«»").strip()
    return topic[:_MAX_TOPIC_CHARS]


def detect_research_topic(text: str) -> str | None:
    """Return the requested topic if ``text`` asks Persona to look something
    up, else ``None``.

    A pure, explicit pattern match — see the module docstring for why this
    is deliberately not an LLM call.
    """
    if not text:
        return None
    match = _REQUEST_RE.search(text.strip())
    if match is None:
        return None
    topic = _clean_topic(match.group("topic"))
    if not topic:
        return None
    return topic


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    """A recorded research task: what to look up, and where to answer."""

    topic: str
    chat_id: int
    sender: str
    source_scope: str

    def __post_init__(self) -> None:
        if not self.topic.strip():
            raise ValueError("research request topic cannot be empty")


def build_research_request(
    text: str,
    *,
    chat_id: int,
    sender: str,
    source_scope: str,
) -> ResearchRequest | None:
    """Detect a research request in ``text`` and package it with the chat
    context that must be preserved: which chat asked, who asked, and that
    chat's privacy scope (so the eventual answer can honour it)."""
    topic = detect_research_topic(text)
    if topic is None:
        return None
    return ResearchRequest(
        topic=topic,
        chat_id=chat_id,
        sender=sender,
        source_scope=source_scope,
    )


__all__ = ["ResearchRequest", "build_research_request", "detect_research_topic"]
