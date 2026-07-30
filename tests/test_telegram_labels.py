"""Every non-owner label shape the codebase can write into
``chat_message.content`` must be recognised by
``app.integrations.telegram.labels.is_untrusted_group_message`` — the one
shared detector ``app.chat.reflection`` (dream-cycle fact promotion) and
``app.thinking.evidence`` (thinking-loop evidence gathering) both rely on to
keep other people's Telegram-group words from being treated as the owner's.

These assertions are driven from the real label builders/call sites rather
than hand-copied strings, so a future change to a builder's prefix shape
fails this test instead of silently reopening the leak.
"""

from __future__ import annotations

from app.integrations.telegram import ambient, labels, service


def test_group_message_label_is_recognised_as_untrusted() -> None:
    built = labels.group_message_label("Alice", "hello there")
    assert labels.is_untrusted_group_message(built)


def test_passive_group_message_label_is_recognised_as_untrusted() -> None:
    built = labels.passive_group_message_label("Alice", "hello there")
    assert labels.is_untrusted_group_message(built)


def test_owner_dm_text_is_not_flagged() -> None:
    assert not labels.is_untrusted_group_message("plain owner message, no label")


def test_ambient_labelled_message_matches_shared_detector() -> None:
    """``TelegramAmbientTurnAdapter``'s stored/persisted string — built by
    ``ambient._labelled_message`` — must be caught by the shared detector."""

    class _FakeTurn:
        sender_label = "Bob"
        text = "some group chatter"

    stored = ambient._labelled_message(_FakeTurn())
    assert labels.is_untrusted_group_message(stored)
    # And it must use the shared builder, not a re-hand-copied prefix.
    assert stored == labels.group_message_label("Bob", "some group chatter")


def test_service_group_turn_text_matches_shared_detector() -> None:
    """``PersonaTelegramService.respond``'s group-turn text-building branch
    must produce exactly what ``labels.group_message_label`` would, so the
    shared detector catches it."""

    sender_label = "Carol"
    clean = "group question"
    built = labels.group_message_label(sender_label, clean)
    assert labels.is_untrusted_group_message(built)
    # Guard against re-divergence: service.py must delegate to the same
    # builder rather than keep its own literal prefix.
    assert service.group_message_label is labels.group_message_label


def test_all_known_prefixes_are_exercised_by_the_real_builders() -> None:
    """Fails loudly if a new label shape is added to ``labels.py`` without a
    real builder producing it, or if a builder's output stops matching one
    of the declared prefixes."""

    produced = {
        labels.group_message_label("X", "t"),
        labels.passive_group_message_label("X", "t"),
    }
    for prefix in labels.UNTRUSTED_LABEL_PREFIXES:
        assert any(text.startswith(prefix) for text in produced), (
            f"no real builder produces a message matching prefix {prefix!r}"
        )
    for text in produced:
        assert labels.is_untrusted_group_message(text)
