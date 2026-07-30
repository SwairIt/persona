from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.integrations.telegram.pinned_ingest import (
    PinnedTelegramConfig,
    PinnedTelegramIngestor,
    _inside_night,
)
from app.integrations.telegram.repository import TelegramRepository


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


async def test_sync_with_owner_prefs_imports_chosen_chat_not_pinned(db) -> None:
    await _user(db)
    chosen_entity = object()
    pinned_entity = object()
    client = FakeClient(
        [
            FakeDialog(-10, "Pinned", True, pinned_entity),
            FakeDialog(-30, "Chosen", False, chosen_entity),
        ],
        {
            id(pinned_entity): [
                FakeMessage(1, "Do not import", FakeEntity(300, "Other"))
            ],
            id(chosen_entity): [
                FakeMessage(1, "Import me", FakeEntity(200, "Oleg", "oleg")),
            ],
        },
    )
    repo = TelegramRepository()
    await repo.set_chat_pref(-30, mode="reply", ingest=True, title="Chosen")
    config = PinnedTelegramConfig(1, "hash", Path("unused"))
    ingestor = PinnedTelegramIngestor(config)

    result = await ingestor.sync_once(client, 7)

    assert result == {"chats": 1, "imported": 1}
    rows = await (
        await db.execute(
            "SELECT telegram_chat_id, telegram_message_id "
            "FROM telegram_pinned_message ORDER BY telegram_message_id"
        )
    ).fetchall()
    assert [(row["telegram_chat_id"], row["telegram_message_id"]) for row in rows] == [
        (-30, 1),
    ]


async def test_sync_without_owner_prefs_falls_back_to_pinned(db) -> None:
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
                FakeMessage(1, "Hello", FakeEntity(100, "Yaroslav", "swairit")),
            ],
            id(other_entity): [
                FakeMessage(1, "Do not import", FakeEntity(300, "Other"))
            ],
        },
    )
    config = PinnedTelegramConfig(1, "hash", Path("unused"))
    ingestor = PinnedTelegramIngestor(config)

    result = await ingestor.sync_once(client, 7)

    assert result == {"chats": 1, "imported": 1}
    rows = await (
        await db.execute(
            "SELECT telegram_chat_id FROM telegram_pinned_message"
        )
    ).fetchall()
    assert [row["telegram_chat_id"] for row in rows] == [-10]


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


def test_config_loads_user_credentials_from_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PERSONA_TG_USER_API_ID", raising=False)
    monkeypatch.delenv("PERSONA_TG_USER_API_HASH", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PERSONA_TG_USER_API_ID=12345678\n"
        "PERSONA_TG_USER_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="utf-8",
    )

    config = PinnedTelegramConfig.load(env_file)

    assert config.configured is True
    assert config.api_id == 12345678
    assert config.api_hash == "0123456789abcdef0123456789abcdef"
    assert config.api_hash not in repr(config)
