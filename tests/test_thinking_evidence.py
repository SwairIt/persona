"""Owner identity in the evidence block (owner mandate 2026-07-30).

Production bug: the evidence block handed the model owner messages/facts
that mention the owner by NAME, but never said who the owner actually is —
so qwen2.5:7b treated "Ярослав" as a third party and built a whole thought
chain theorising about the owner's dissatisfaction with "Ярослав", not
realising they are the same person. These tests pin the fix: the block
must name the owner when a name is resolvable, say "владелец" when it
isn't, and always state the named-person-is-the-owner rule.
"""

from __future__ import annotations

from app.integrations.telegram.people import TelegramPeopleRepository
from app.thinking.evidence import gather_evidence


async def _user(db, user_id: int = 7) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


async def _owner_message(db, user_id: int, text: str) -> None:
    cur = await db.execute(
        "INSERT INTO chat_session(user_id, title) VALUES(?, 't')", (user_id,)
    )
    session_id = cur.lastrowid
    await db.execute(
        "INSERT INTO chat_message(session_id, role, content) VALUES(?, 'user', ?)",
        (session_id, text),
    )
    await db.commit()


async def test_evidence_names_the_owner_when_resolvable(db) -> None:
    await _user(db)
    await _owner_message(db, 7, "Сегодня был длинный день.")
    await db.execute(
        "INSERT INTO telegram_person(persona_user_id, telegram_user_id, "
        "display_name, is_owner) VALUES(7, 1000000001, 'Ярослав', 1)"
    )
    await db.commit()

    evidence = await gather_evidence(7)
    assert "Ярослав" in evidence
    assert "тот же самый" in evidence
    assert "не третье лицо" in evidence


async def test_evidence_falls_back_to_generic_owner_label(db) -> None:
    await _user(db)
    await _owner_message(db, 7, "Сегодня был длинный день.")
    # No telegram_person row at all — no name resolvable.
    evidence = await gather_evidence(7)
    assert "владелец" in evidence.lower()
    assert "тот же самый" in evidence


async def test_evidence_prefers_owner_authored_override_name(db) -> None:
    """The override the owner sets on /settings/telegram-people must win
    over the raw Telegram display_name, mirroring identity_context()."""
    await _user(db)
    await _owner_message(db, 7, "Сегодня был длинный день.")
    await db.execute(
        "INSERT INTO telegram_person(persona_user_id, telegram_user_id, "
        "display_name, is_owner) VALUES(7, 1000000001, 'RawTelegramName', 1)"
    )
    await db.commit()
    await TelegramPeopleRepository().set_override(
        7, 1000000001, display_name="Ярослав", note="", ignored=False
    )

    evidence = await gather_evidence(7)
    assert "Ярослав" in evidence
    assert "RawTelegramName" not in evidence


async def test_no_evidence_still_returns_empty(db) -> None:
    """The identity line must never be the ONLY content — with no owner
    messages and no facts, seed_chain must still see "" and refuse to seed
    (tests/test_thinking_loop.py::test_evidence_dependent_kind_with_no_evidence_seeds_nothing)."""
    await _user(db)
    assert await gather_evidence(7) == ""
