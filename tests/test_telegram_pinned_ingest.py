from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.integrations.telegram.pinned_ingest import (
    PinnedTelegramConfig,
    PinnedTelegramIngestor,
    _inside_night,
)


@dataclass
class FakeEntity:
    id: int
    first_name: str
    username: str = ""
    bot: bool = False


@dataclass
class FakeMessage:
    id: int
    raw_text: str
    sender: FakeEntity
    date: datetime = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)

    @property
    def sender_id(self) -> int:
        return self.sender.id

    async def get_sender(self) -> FakeEntity:
        return self.sender


@dataclass
class FakeDialog:
    id: int
    name: str
    pinned: bool
    entity: object


class FakeClient:
    def __init__(self, dialogs: list[FakeDialog], messages: dict[int, list[FakeMessage]]):
        self.dialogs = dialogs
        self.messages = messages

    async def get_me(self) -> FakeEntity:
        return FakeEntity(100, "Yaroslav", "swairit")

    async def get_dialogs(self, *, limit: int | None = None) -> list[FakeDialog]:
        del limit
        return self.dialogs

    async def iter_messages(
        self,
        entity: object,
        *,
        min_id: int,
        reverse: bool,
        limit: int,
    ):
        del reverse
        for message in self.messages.get(id(entity), [])[:limit]:
            if message.id > min_id:
                yield message


async def _user(db, user_id: int = 7) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


async def test_sync_imports_only_pinned_dialogs_and_is_idempotent(db) -> None:
    await _user(db)
    pinned_entity = object()
    other_entity = object()
    client = FakeClient(
        [
            FakeDialog(-10, "Pinned", True, pinned_entity),
            FakeDialog(-20, "Other", False, other_entity),
        ],
        {
            id(pinned_entity): [
                FakeMessage(2, "I like tea", FakeEntity(200, "Oleg", "oleg")),
                FakeMessage(1, "Hello", FakeEntity(100, "Yaroslav", "swairit")),
            ],
            id(other_entity): [
                FakeMessage(1, "Do not import", FakeEntity(300, "Other"))
            ],
        },
    )
    config = PinnedTelegramConfig(1, "hash", Path("unused"))
    ingestor = PinnedTelegramIngestor(config)

    first = await ingestor.sync_once(client, 7)
    second = await ingestor.sync_once(client, 7)

    assert first == {"chats": 1, "imported": 2}
    assert second == {"chats": 1, "imported": 0}
    rows = await (
        await db.execute(
            "SELECT telegram_chat_id, telegram_message_id, text "
            "FROM telegram_pinned_message ORDER BY telegram_message_id"
        )
    ).fetchall()
    assert [(row["telegram_chat_id"], row["telegram_message_id"]) for row in rows] == [
        (-10, 1),
        (-10, 2),
    ]


def test_night_window_handles_normal_and_wrapped_ranges() -> None:
    normal = PinnedTelegramConfig(1, "h", Path("x"), night_start_hour=0, night_end_hour=7)
    wrapped = PinnedTelegramConfig(
        1, "h", Path("x"), night_start_hour=22, night_end_hour=6
    )
    assert _inside_night(3, normal)
    assert not _inside_night(12, normal)
    assert _inside_night(23, wrapped)
    assert _inside_night(4, wrapped)
    assert not _inside_night(12, wrapped)
