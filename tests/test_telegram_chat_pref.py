from __future__ import annotations

from app.integrations.telegram.repository import TelegramRepository


async def test_set_and_get_chat_pref_round_trips(db) -> None:
    del db
    repo = TelegramRepository()

    await repo.set_chat_pref(-100, mode="read", ingest=True, title="Some Group")

    pref = await repo.chat_pref(-100)
    assert pref is not None
    assert pref["telegram_chat_id"] == -100
    assert pref["title"] == "Some Group"
    assert pref["mode"] == "read"
    assert pref["ingest"] is True


async def test_unknown_chat_pref_returns_none(db) -> None:
    del db
    repo = TelegramRepository()

    assert await repo.chat_pref(-999) is None


async def test_reply_mode_adds_to_allowed_chat_ids(db) -> None:
    del db
    repo = TelegramRepository()

    await repo.set_chat_pref(-200, mode="reply", ingest=False, title="Reply Group")

    assert -200 in await repo.allowed_chat_ids()


async def test_non_reply_mode_removes_from_allowed_chat_ids(db) -> None:
    del db
    repo = TelegramRepository()

    await repo.set_chat_pref(-300, mode="reply", ingest=False, title="Group")
    assert -300 in await repo.allowed_chat_ids()

    await repo.set_chat_pref(-300, mode="read", ingest=False, title="Group")
    assert -300 not in await repo.allowed_chat_ids()

    await repo.set_chat_pref(-300, mode="reply", ingest=False, title="Group")
    await repo.set_chat_pref(-300, mode="ignore", ingest=False, title="Group")
    assert -300 not in await repo.allowed_chat_ids()


async def test_ingest_chat_ids_returns_only_ingest_flagged_chats(db) -> None:
    del db
    repo = TelegramRepository()

    await repo.set_chat_pref(-400, mode="read", ingest=True, title="A")
    await repo.set_chat_pref(-401, mode="read", ingest=False, title="B")
    await repo.set_chat_pref(-402, mode="reply", ingest=True, title="C")

    ids = await repo.ingest_chat_ids()
    assert ids == {-400, -402}


async def test_list_chat_prefs_returns_all_rows(db) -> None:
    del db
    repo = TelegramRepository()

    await repo.set_chat_pref(-500, mode="read", ingest=True, title="X")
    await repo.set_chat_pref(-501, mode="ignore", ingest=False, title="Y")

    prefs = await repo.list_chat_prefs()
    ids = {pref["telegram_chat_id"] for pref in prefs}
    assert {-500, -501} <= ids
