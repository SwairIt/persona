"""A concluded ``research`` chain must be delivered back into the chat that
asked -- never into the owner's private DM/diary as owner-private data when
the request came from a group.
"""
# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.adapters.autowake import SqliteAutowakeRepository
from app.application.autowake import AutowakeService, enqueue_completed_research
from app.domains.autowake import SourceScope

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _service() -> AutowakeService:
    repository = SqliteAutowakeRepository()
    return AutowakeService(repository, expected_owner_user_id=7)


async def test_group_sourced_research_is_delivered_to_the_originating_chat(db) -> None:
    result = await enqueue_completed_research(
        _service(),
        owner_user_id=7,
        chain_id=42,
        topic="Лабиринт Фавна",
        conclusion="По прочитанному это тёмная сказка о взрослении.",
        completed_at=NOW,
        source_scope=SourceScope.GROUP,
        chat_id=-100500,
    )
    assert result.created and result.accepted

    row = await (
        await db.execute(
            "SELECT channel, target_chat_id FROM autowake_outbox WHERE owner_user_id=7"
        )
    ).fetchone()
    assert row["channel"] == "telegram_group"
    assert row["target_chat_id"] == -100500


async def test_owner_private_research_falls_back_to_the_owner_dm(db) -> None:
    result = await enqueue_completed_research(
        _service(),
        owner_user_id=7,
        chain_id=43,
        topic="Лабиринт Фавна",
        conclusion="По прочитанному это тёмная сказка о взрослении.",
        completed_at=NOW,
        source_scope=SourceScope.DERIVED_OWNER,
        chat_id=None,
    )
    assert result.created and result.accepted

    row = await (
        await db.execute(
            "SELECT channel, target_chat_id FROM autowake_outbox WHERE owner_user_id=7"
        )
    ).fetchone()
    assert row["channel"] == "telegram_owner_dm"
    assert row["target_chat_id"] is None


async def test_group_delivery_requires_a_real_negative_chat_id(db) -> None:
    with pytest.raises(ValueError, match="chat id"):
        await enqueue_completed_research(
            _service(),
            owner_user_id=7,
            chain_id=44,
            topic="Лабиринт Фавна",
            conclusion="вывод",
            completed_at=NOW,
            source_scope=SourceScope.GROUP,
            chat_id=None,
        )
