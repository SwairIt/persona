"""Storage for Persona's self-directed thought chains.

Follows the conventions of ``app.integrations.telegram.people``: writes go
through ``write_transaction()`` (a single ``BEGIN IMMEDIATE`` transaction per
call), reads go through ``get_connection()``, and rows are returned as plain
``dict`` copies of ``aiosqlite.Row`` rather than bespoke objects.
"""

from __future__ import annotations

from typing import Any

from app.storage.db import get_connection, write_transaction

_MAX_TEXT_CHARS = 4000


def _clip(text: str) -> str:
    """Clip stored text to the shared 4000-char bound."""
    return str(text or "")[:_MAX_TEXT_CHARS]


class ThoughtStore:
    async def open_chain(
        self,
        persona_user_id: int,
        *,
        seed_text: str,
        seed_kind: str,
        source_scope: str,
        source_session_id: int | None,
    ) -> int:
        """Open a new chain and write its seed row (step_no=0) atomically.

        A chain with no seed is a broken state, so both inserts happen inside
        one ``write_transaction()``.
        """
        tenant = int(persona_user_id)
        session_id = int(source_session_id) if source_session_id is not None else None
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO persona_thought_chain(
                    persona_user_id, seed_kind, source_scope, source_session_id
                )
                VALUES(?,?,?,?)
                """,
                (tenant, seed_kind, source_scope, session_id),
            )
            chain_id = int(cursor.lastrowid)
            await conn.execute(
                """
                INSERT INTO persona_thought(
                    persona_user_id, chain_id, step_no, kind, seed_kind, text,
                    source_scope, source_session_id
                )
                VALUES(?,?,0,'seed',?,?,?,?)
                """,
                (
                    tenant,
                    chain_id,
                    seed_kind,
                    _clip(seed_text),
                    source_scope,
                    session_id,
                ),
            )
        return chain_id

    async def append_step(self, chain_id: int, *, text: str) -> int:
        """Append the next step, allocating ``step_no`` inside the same
        transaction as the insert so two concurrent appends cannot collide on
        ``UNIQUE (chain_id, step_no)``.
        """
        chain = int(chain_id)
        async with write_transaction() as conn:
            info = await self._chain_info(conn, chain)
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(step_no), -1) AS max_step "
                "FROM persona_thought WHERE chain_id=?",
                (chain,),
            )
            row = await cursor.fetchone()
            step_no = int(row["max_step"]) + 1
            await conn.execute(
                """
                INSERT INTO persona_thought(
                    persona_user_id, chain_id, step_no, kind, seed_kind, text,
                    source_scope, source_session_id
                )
                VALUES(?,?,?,'step',?,?,?,?)
                """,
                (
                    info["persona_user_id"],
                    chain,
                    step_no,
                    info["seed_kind"],
                    _clip(text),
                    info["source_scope"],
                    info["source_session_id"],
                ),
            )
        return step_no

    async def close_chain(self, chain_id: int, *, conclusion: str) -> None:
        """Write the conclusion row and flip the chain to 'closed' atomically."""
        chain = int(chain_id)
        async with write_transaction() as conn:
            info = await self._chain_info(conn, chain)
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(step_no), -1) AS max_step "
                "FROM persona_thought WHERE chain_id=?",
                (chain,),
            )
            row = await cursor.fetchone()
            step_no = int(row["max_step"]) + 1
            await conn.execute(
                """
                INSERT INTO persona_thought(
                    persona_user_id, chain_id, step_no, kind, seed_kind, text,
                    source_scope, source_session_id
                )
                VALUES(?,?,?,'conclusion',?,?,?,?)
                """,
                (
                    info["persona_user_id"],
                    chain,
                    step_no,
                    info["seed_kind"],
                    _clip(conclusion),
                    info["source_scope"],
                    info["source_session_id"],
                ),
            )
            await conn.execute(
                """
                UPDATE persona_thought_chain
                   SET status='closed', closed_at=datetime('now')
                 WHERE chain_id=?
                """,
                (chain,),
            )

    async def oldest_open_chain(self, persona_user_id: int) -> dict[str, Any] | None:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT chain_id, persona_user_id, seed_kind, source_scope,
                       source_session_id, status, created_at, closed_at
                  FROM persona_thought_chain
                 WHERE persona_user_id=? AND status='open'
                 ORDER BY created_at ASC, chain_id ASC
                 LIMIT 1
                """,
                (int(persona_user_id),),
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def chain_steps(self, chain_id: int) -> list[dict[str, Any]]:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, persona_user_id, chain_id, step_no, kind, seed_kind,
                       text, source_scope, source_session_id, created_at
                  FROM persona_thought
                 WHERE chain_id=?
                 ORDER BY step_no ASC
                """,
                (int(chain_id),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def steps_used_today(self, persona_user_id: int) -> int:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT COUNT(*) AS n
                  FROM persona_thought
                 WHERE persona_user_id=? AND date(created_at) = date('now')
                """,
                (int(persona_user_id),),
            )
            row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0

    @staticmethod
    async def _chain_info(conn: Any, chain_id: int) -> dict[str, Any]:
        cursor = await conn.execute(
            """
            SELECT persona_user_id, seed_kind, source_scope, source_session_id
              FROM persona_thought_chain
             WHERE chain_id=?
            """,
            (chain_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"no such thought chain: {chain_id}")
        return dict(row)


__all__ = ["ThoughtStore"]
