"""Single source of truth for the label prefixes Telegram integrations write
into ``chat_message.content`` for non-owner speech.

Two independent consumers must agree on exactly which stored strings mean
"someone other than the owner said this" — ``app.chat.reflection`` (the
nightly dream cycle that promotes extracted facts into long-term memory) and
``app.thinking.evidence`` (the thinking loop's evidence gatherer). They used
to keep separate hand-copied prefix constants; when the group-turn builders
below grew a second label shape (``"[Telegram group · "``), only one of the
two constants was updated, and messages written by other group participants
started being treated as if the owner had said them. This module exists so
there is exactly one place that knows the label shapes and one place that
recognises them — add a new label builder here and the detector covers it
automatically.

This module has no memory-writer dependency: it is pure string handling, safe
for ``app.thinking`` (which must never gain a memory write path — see
``tests/test_thinking_no_memory_writes.py``) to import.
"""

from __future__ import annotations

# Every prefix a message stored in ``chat_message.content`` can carry when it
# was written by someone other than the owner. Order does not matter for
# correctness, but longer/more-specific prefixes are listed first for
# readability.
GROUP_LABEL_PREFIX = "[Telegram group · "
PASSIVE_GROUP_LABEL_PREFIX = "[Telegram · "

UNTRUSTED_LABEL_PREFIXES: tuple[str, ...] = (
    GROUP_LABEL_PREFIX,
    PASSIVE_GROUP_LABEL_PREFIX,
)


def group_message_label(sender_label: str, text: str) -> str:
    """Label an addressed/ambient group turn: ``"[Telegram group · X] text"``."""
    return f"{GROUP_LABEL_PREFIX}{sender_label[:120]}] {text.strip()}"


def passive_group_message_label(sender_label: str, text: str) -> str:
    """Label a passively-recorded group message: ``"[Telegram · X] text"``."""
    return f"{PASSIVE_GROUP_LABEL_PREFIX}{sender_label}] {text.strip()}"


def is_untrusted_group_message(text: str) -> bool:
    """Return whether a stored chat row came from a Telegram group participant
    other than the owner.

    Fail closed: any stored content wearing one of the known non-owner label
    prefixes must never be treated as something the owner said — not
    promoted into ``user_memory`` facts, not handed to the thinking loop as
    owner evidence.
    """
    stripped = (text or "").lstrip()
    return any(stripped.startswith(prefix) for prefix in UNTRUSTED_LABEL_PREFIXES)


__all__ = [
    "GROUP_LABEL_PREFIX",
    "PASSIVE_GROUP_LABEL_PREFIX",
    "UNTRUSTED_LABEL_PREFIXES",
    "group_message_label",
    "passive_group_message_label",
    "is_untrusted_group_message",
]
