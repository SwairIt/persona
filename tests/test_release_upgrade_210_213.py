"""Upgrade contract from the already-applied 210/211 production schema."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from app.storage.migration_runner import migrate

if TYPE_CHECKING:
    from collections.abc import Iterable

_CHECKSUM_210 = "e01648391f572def0bc1b7e9d5c42c9ca468d3e6257403a8bd94f06645557144"
_CHECKSUM_211 = "84083405e30be8258fb90d8f8d7c645ae7d7a9fe4c0f7eb76d6ae817dd0d95b4"

_BASE_SCHEMA = """
CREATE TABLE users(id INTEGER PRIMARY KEY);
CREATE TABLE user_memory(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    valid_until TEXT
);
CREATE TABLE dream_run(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE dream_candidate(
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES dream_run(id) ON DELETE CASCADE,
    status TEXT NOT NULL
);
CREATE TABLE dream_evidence(
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES dream_candidate(id) ON DELETE CASCADE
);
CREATE TABLE dream_revision(
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES dream_run(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES dream_candidate(id) ON DELETE CASCADE,
    memory_id INTEGER REFERENCES user_memory(id) ON DELETE CASCADE,
    action TEXT NOT NULL
);
CREATE TABLE kg_entity(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL
);
CREATE TABLE kg_edge(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    from_entity_id INTEGER REFERENCES kg_entity(id) ON DELETE CASCADE,
    to_entity_id INTEGER REFERENCES kg_entity(id) ON DELETE CASCADE
);
CREATE TABLE worker_heartbeat(
    name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    last_status TEXT,
    ticks INTEGER NOT NULL DEFAULT 0
);
"""


async def _run_upgrade(db_path: Path, migrations_dir: Path) -> None:
    async with aiosqlite.connect(db_path, isolation_level=None) as conn:
        await conn.execute("PRAGMA foreign_keys = ON")
        await migrate(
            conn,
            schema_sql=_BASE_SCHEMA,
            migrations_dir=migrations_dir,
            sqlite_vec_loaded=False,
        )


def _copy_migrations(
    source: Path,
    target: Path,
    names: Iterable[str],
) -> None:
    for name in names:
        (target / name).write_bytes((source / name).read_bytes())


@pytest.mark.asyncio
async def test_historical_210_211_upgrade_preserves_data_and_repairs_delete(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[1] / "app" / "storage" / "migrations"
    staged = tmp_path / "migrations"
    staged.mkdir()
    db_path = tmp_path / "historical-211.db"

    _copy_migrations(
        source,
        staged,
        ("210_memory_projection_outbox.sql", "211_worker_enrollment.sql"),
    )
    await _run_upgrade(db_path, staged)

    async with aiosqlite.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        ledger = await (
            await conn.execute(
                """
                SELECT migration_order, checksum FROM schema_migration
                 WHERE migration_order IN (210, 211)
                 ORDER BY migration_order
                """
            )
        ).fetchall()
        assert [tuple(row) for row in ledger] == [
            (210, _CHECKSUM_210),
            (211, _CHECKSUM_211),
        ]

        await conn.executemany("INSERT INTO users(id) VALUES(?)", ((1,), (2,)))
        await conn.executemany(
            "INSERT INTO user_memory(id,user_id,text) VALUES(?,?,?)",
            ((11, 1, "owner memory"), (22, 2, "other memory")),
        )
        await conn.execute("INSERT INTO dream_run(id,user_id) VALUES(10,1)")
        await conn.executemany(
            "INSERT INTO dream_candidate(id,run_id,status) VALUES(?,?,?)",
            ((20, 10, "applied"), (21, 10, "applied")),
        )
        await conn.executemany(
            "INSERT INTO dream_evidence(id,candidate_id) VALUES(?,?)",
            ((30, 20), (31, 21)),
        )
        await conn.execute(
            """
            INSERT INTO dream_revision(id,run_id,candidate_id,memory_id,action)
            VALUES(40,10,20,11,'add')
            """
        )
        await conn.execute(
            """
            INSERT INTO memory_projection_outbox(
                id,owner_user_id,dream_revision_id,memory_id,
                projection_kind,content_hash
            ) VALUES(1,1,40,11,'graph','content-hash')
            """
        )
        await conn.execute(
            "INSERT INTO memory_projection_evidence(outbox_id,evidence_id) VALUES(1,30)"
        )
        await conn.execute(
            """
            INSERT INTO memory_revision_embedding(
                dream_revision_id,owner_user_id,memory_id,content_hash,
                model_name,dimensions,embedding
            ) VALUES(40,1,11,'content-hash','historical',1,X'00000000')
            """
        )
        await conn.executemany(
            "INSERT INTO kg_entity(id,user_id) VALUES(?,1)",
            ((100,), (101,)),
        )
        await conn.execute(
            """
            INSERT INTO kg_edge(id,user_id,from_entity_id,to_entity_id)
            VALUES(200,1,100,101)
            """
        )
        await conn.execute(
            """
            INSERT INTO graph_revision_projection(
                dream_revision_id,triple_hash,kg_edge_id
            ) VALUES(40,'triple-hash',200)
            """
        )
        await conn.executemany(
            """
            INSERT INTO worker_enrollment_ticket(
                id,ticket_hash,owner_user_id,status,issued_at,expires_at,
                consumed_at,consumed_worker_id,revoked_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                (1, "a" * 64, 1, "issued", "t1", "t2", None, None, None),
                (2, "b" * 64, 1, "expired", "t1", "t2", None, None, None),
                (3, "c" * 64, 1, "consumed", "t1", "t2", "t2", "pc-1", None),
            ),
        )

    _copy_migrations(
        source,
        staged,
        ("212_projection_source_guards.sql", "213_worker_enrollment_cascade.sql"),
    )
    await _run_upgrade(db_path, staged)
    await _run_upgrade(db_path, staged)

    async with aiosqlite.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        # Production had already applied the historical 212/213 bytes before
        # their later hardening was authored. Apply the append-only corrections
        # directly here; the full-manifest ordering is covered by the normal DB
        # fixture and the production-copy migration probe.
        await conn.executescript(
            (source / "216_projection_source_guard_hardening.sql").read_text(
                encoding="utf-8"
            )
        )
        await conn.executescript(
            (source / "217_worker_enrollment_activation_expiry.sql").read_text(
                encoding="utf-8"
            )
        )
        historical = await (
            await conn.execute(
                """
                SELECT id,status,revoked_at,pending_llm_token_hash,
                       pending_browser_token_hash,activation_expires_at,
                       activated_at
                  FROM worker_enrollment_ticket
                 ORDER BY id
                """
            )
        ).fetchall()
        assert [tuple(row) for row in historical] == [
            (1, "issued", None, None, None, None, None),
            (2, "expired", "t1", None, None, None, None),
            (3, "consumed", None, None, None, None, None),
        ]
        foreign_keys = await (
            await conn.execute("PRAGMA foreign_key_list(worker_enrollment_ticket)")
        ).fetchall()
        owner_fk = next(row for row in foreign_keys if row["from"] == "owner_user_id")
        assert owner_fk["on_delete"] == "CASCADE"
        durable_objects = {
            str(row["name"])
            for row in await (
                await conn.execute(
                    """
                    SELECT name FROM sqlite_master
                     WHERE type IN ('index', 'trigger')
                    """
                )
            ).fetchall()
        }
        assert {
            "idx_memory_projection_owner_due",
            "idx_memory_projection_owner_lease",
            "idx_worker_enrollment_active",
            "idx_worker_enrollment_recent",
            "idx_worker_enrollment_pending_llm",
            "idx_worker_enrollment_pending_browser",
            "idx_worker_enrollment_pending_activation",
            "worker_enrollment_identity_immutable",
            "memory_projection_outbox_owner_guard_insert",
            "memory_projection_outbox_owner_guard_update",
            "memory_projection_evidence_guard",
            "memory_projection_evidence_guard_update",
        } <= durable_objects

        with pytest.raises(
            aiosqlite.IntegrityError,
            match="projection owner/source mismatch",
        ):
            await conn.execute(
                """
                INSERT INTO memory_projection_outbox(
                    owner_user_id,dream_revision_id,memory_id,
                    projection_kind,projector_version,content_hash
                ) VALUES(2,40,11,'embedding',99,'bad-owner')
                """
            )
        with pytest.raises(
            aiosqlite.IntegrityError,
            match="projection evidence/source mismatch",
        ):
            await conn.execute(
                """
                INSERT INTO memory_projection_evidence(outbox_id,evidence_id)
                VALUES(1,31)
                """
            )
        with pytest.raises(
            aiosqlite.IntegrityError,
            match="projection evidence/source mismatch",
        ):
            await conn.execute(
                """
                UPDATE memory_projection_evidence
                   SET evidence_id=31
                 WHERE outbox_id=1 AND evidence_id=30
                """
            )
        with pytest.raises(aiosqlite.IntegrityError, match="CHECK constraint"):
            await conn.execute(
                """
                INSERT INTO worker_enrollment_ticket(
                    ticket_hash,owner_user_id,status,issued_at,expires_at
                ) VALUES(? ,1,'expired','t1','t2')
                """,
                ("d" * 64,),
            )
        with pytest.raises(aiosqlite.IntegrityError, match="CHECK constraint"):
            await conn.execute(
                """
                INSERT INTO worker_enrollment_ticket(
                    ticket_hash,owner_user_id,status,issued_at,expires_at,
                    consumed_at,consumed_worker_id,pending_llm_token_hash
                ) VALUES(?,1,'consumed','t1','t2','t2','pc-2',?)
                """,
                ("e" * 64, "f" * 64),
            )

        await conn.execute(
            """
            INSERT INTO worker_enrollment_ticket(
                ticket_hash,owner_user_id,status,issued_at,expires_at,
                consumed_at,consumed_worker_id,pending_llm_token_hash,
                pending_browser_token_hash,activation_expires_at
            ) VALUES(?,1,'consumed','t1','t2','t2','pc-2',?,?,'t4')
            """,
            ("e" * 64, "f" * 64, "9" * 64),
        )
        await conn.execute(
            """
            UPDATE worker_enrollment_ticket SET activated_at='t3'
             WHERE ticket_hash=?
            """,
            ("e" * 64,),
        )

        await conn.execute("DELETE FROM users WHERE id=1")

        for table in (
            "worker_enrollment_ticket",
            "memory_projection_evidence",
            "memory_revision_embedding",
            "graph_revision_projection",
            "memory_projection_outbox",
            "kg_edge",
            "kg_entity",
        ):
            count = await (await conn.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            assert count[0] == 0, table
        remaining_users = await (
            await conn.execute("SELECT id FROM users ORDER BY id")
        ).fetchall()
        assert [row["id"] for row in remaining_users] == [2]
