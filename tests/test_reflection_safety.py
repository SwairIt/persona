"""Safety regressions for the nightly reflection cursor and source trust."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.chat import reflection

if TYPE_CHECKING:
    import aiosqlite


async def _user(db: aiosqlite.Connection, user_id: int = 1) -> None:
    await db.execute(
        "INSERT INTO users(id, email, password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


async def _session(db: aiosqlite.Connection, user_id: int, title: str) -> int:
    cur = await db.execute(
        "INSERT INTO chat_session(user_id, title) VALUES(?,?)",
        (user_id, title),
    )
    await db.commit()
    return int(cur.lastrowid)


async def _message(db: aiosqlite.Connection, session_id: int, text: str) -> int:
    cur = await db.execute(
        "INSERT INTO chat_message(session_id, role, content) VALUES(?, 'user', ?)",
        (session_id, text),
    )
    await db.commit()
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_gather_excludes_group_speech_but_keeps_owner_telegram_dm(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    group_session = await _session(db, 1, "Telegram · Project group")
    dm_session = await _session(db, 1, "Telegram · owner")
    group_id = await _message(
        db,
        group_session,
        "[Telegram · Alice] Меня зовут Алиса, я живу в Казани",
    )
    dm_id = await _message(
        db,
        dm_session,
        "Меня зовут Ярослав, я делаю Persona",
    )

    docs, scanned, ignored = await reflection._gather_documents(1, "2000-01-01 00:00:00", 0)

    chat_docs = [doc for doc in docs if doc["source"].startswith("chat:")]
    assert scanned == {group_id, dm_id}
    assert ignored == {group_id}
    assert len(chat_docs) == 1
    assert chat_docs[0]["message_ids"] == [dm_id]
    assert "Ярослав" in chat_docs[0]["text"]
    assert "Алиса" not in chat_docs[0]["text"]


@pytest.mark.asyncio
async def test_cursor_stops_before_document_omitted_by_run_budget(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _user(db)
    message_ids: list[int] = []
    for index in range(3):
        session_id = await _session(db, 1, f"session-{index}")
        message_ids.append(await _message(db, session_id, f"Важный факт номер {index}"))

    monkeypatch.setattr(reflection, "_MAX_DOCS", 2)
    docs, scanned, ignored = await reflection._gather_documents(1, "2000-01-01 00:00:00", 0)
    selected_ids = {int(mid) for doc in docs for mid in (doc.get("message_ids") or [])}
    cursor = reflection._safe_processed_cursor(0, scanned, ignored, selected_ids)

    assert selected_ids == set(message_ids[:2])
    assert cursor == message_ids[2] - 1
    assert cursor < message_ids[2]


@pytest.mark.asyncio
async def test_failed_extraction_does_not_mark_document_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(
        _client: Any,
        _system: str,
        user: str,
        **_kwargs: Any,
    ) -> list[dict[str, str]]:
        if "fail" in user:
            raise RuntimeError("provider unavailable")
        return []

    monkeypatch.setattr("app.chat.user_memory._extract_facts", fake_extract)
    docs = [
        {
            "source": "chat:1",
            "text": "ok",
            "message_ids": [10],
            "latest_at": "2026-07-28 01:00:00",
        },
        {
            "source": "chat:2",
            "text": "fail",
            "message_ids": [11],
            "latest_at": "2026-07-28 01:01:00",
        },
    ]

    candidates, processed = await reflection._light_sleep(object(), docs, 50)

    assert candidates == []
    assert processed == {10}
    assert reflection._safe_processed_cursor(0, {10, 11}, set(), processed) == 10


def test_group_filter_does_not_match_owner_dm() -> None:
    assert reflection._is_untrusted_group_message("[Telegram · Alice] group message")
    assert not reflection._is_untrusted_group_message("Привет, это личное сообщение владельца")
