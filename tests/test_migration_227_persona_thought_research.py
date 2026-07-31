"""Migration 227 rebuilds ``persona_thought`` to accept seed_kind='research'.

SQLite cannot ALTER a CHECK constraint, so the migration renames the table,
creates a new one with 'research' added to the CHECK, copies every row, and
drops the legacy table. This test proves the rebuild is lossless and that
the new value is genuinely accepted (not just present in the .sql text).
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "app" / "storage" / "migrations"


def _read(name: str) -> str:
    return (_MIGRATIONS_DIR / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_migration_227_preserves_rows_and_accepts_research(tmp_path: Path) -> None:
    db_path = tmp_path / "persona-thought-research.db"
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        await db.execute("PRAGMA foreign_keys=OFF")
        # Recreate the pre-227 schema exactly as earlier migrations left it.
        await db.executescript(_read("222_persona_thought.sql"))
        await db.executescript(_read("223_persona_thought_certainty.sql"))
        await db.executescript(_read("224_persona_thought_confirmed.sql"))

        await db.execute(
            "INSERT INTO persona_thought_chain("
            "chain_id, persona_user_id, seed_kind, source_scope, "
            "source_session_id, status) "
            "VALUES(1, 7, 'alive', 'owner_private', NULL, 'open')"
        )
        await db.execute(
            "INSERT INTO persona_thought("
            "id, persona_user_id, chain_id, step_no, kind, seed_kind, text, "
            "source_scope, source_session_id) "
            "VALUES(100, 7, 1, 0, 'seed', 'alive', "
            "'существующая мысль до миграции', 'owner_private', NULL)"
        )

        await db.executescript(_read("227_persona_thought_research.sql"))

        row = await (
            await db.execute(
                "SELECT id, chain_id, text, seed_kind, certainty, source_scope "
                "FROM persona_thought WHERE id=100"
            )
        ).fetchone()
        assert row == (100, 1, "существующая мысль до миграции", "alive", "guess", "owner_private")

        chain_row = await (
            await db.execute(
                "SELECT chain_id, seed_kind, source_chat_id FROM persona_thought_chain WHERE chain_id=1"
            )
        ).fetchone()
        assert chain_row == (1, "alive", None)

        # 'research' is now accepted, with a chat id preserved on the chain.
        await db.execute(
            "INSERT INTO persona_thought_chain("
            "chain_id, persona_user_id, seed_kind, source_scope, "
            "source_session_id, status, source_chat_id) "
            "VALUES(2, 7, 'research', 'group', NULL, 'open', -100500)"
        )
        await db.execute(
            "INSERT INTO persona_thought("
            "persona_user_id, chain_id, step_no, kind, seed_kind, text, "
            "source_scope, source_session_id) "
            "VALUES(7, 2, 0, 'seed', 'research', 'лабиринт фавна', 'group', NULL)"
        )
        research_row = await (
            await db.execute(
                "SELECT seed_kind, text FROM persona_thought WHERE chain_id=2 AND step_no=0"
            )
        ).fetchone()
        assert research_row == ("research", "лабиринт фавна")

        chain2 = await (
            await db.execute(
                "SELECT seed_kind, source_chat_id FROM persona_thought_chain WHERE chain_id=2"
            )
        ).fetchone()
        assert chain2 == ("research", -100500)

        # An unknown seed_kind is still rejected by the CHECK constraint.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO persona_thought("
                "persona_user_id, chain_id, step_no, kind, seed_kind, text, "
                "source_scope, source_session_id) "
                "VALUES(7, 2, 1, 'step', 'not_a_real_kind', 'x', 'group', NULL)"
            )
