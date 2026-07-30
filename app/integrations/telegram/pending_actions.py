"""One-shot parking for execution-class Telegram tool calls.

`claim` decides whether an action actually executes, so its check-and-mark
must be atomic: a read-then-write would leave a window where two concurrent
callbacks (e.g. a double-tap on the inline confirm button) both observe
"not consumed" and both execute. The conditional ``UPDATE ... WHERE
consumed_at IS NULL AND expires_at > datetime('now') AND persona_user_id=?``
inside a single ``write_transaction`` closes that window: SQLite's
``BEGIN IMMEDIATE`` serializes concurrent writers, so exactly one UPDATE
can ever affect a row, and every other caller sees ``rowcount == 0``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.storage.db import get_connection, write_transaction

if TYPE_CHECKING:
    from datetime import datetime

TTL_MINUTES = 15


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: int
    persona_user_id: int
    telegram_chat_id: int
    tool_name: str
    args: dict[str, Any]


class PendingActionStore:
    TTL_MINUTES = TTL_MINUTES

    async def park(
        self,
        persona_user_id: int,
        *,
        tool_name: str,
        args: dict[str, Any],
        chat_id: int,
    ) -> int:
        """Park one execution-class tool call and return its pending id."""
        clean_tool_name = _clean(tool_name, 128)
        encoded_args = json.dumps(dict(args), ensure_ascii=False)
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO telegram_pending_action(
                    persona_user_id, telegram_chat_id, tool_name, args_json,
                    expires_at
                )
                VALUES(?, ?, ?, ?, datetime('now', ?))
                """,
                (
                    int(persona_user_id),
                    int(chat_id),
                    clean_tool_name,
                    encoded_args,
                    f"+{TTL_MINUTES} minutes",
                ),
            )
            pending_id = int(cursor.lastrowid)
        return pending_id

    async def claim(
        self,
        persona_user_id: int,
        pending_id: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Atomically consume one pending action; ``None`` if it cannot be claimed.

        Returns ``None`` when the id is unknown, already consumed, expired,
        or belongs to another tenant. ``now`` is accepted for API symmetry
        with the interface contract; the actual expiry check is evaluated
        by SQLite's own ``datetime('now')`` inside the same statement so
        the comparison and the mark happen in one atomic operation.
        """
        del now
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE telegram_pending_action
                   SET consumed_at = datetime('now')
                 WHERE id = ?
                   AND consumed_at IS NULL
                   AND expires_at > datetime('now')
                   AND persona_user_id = ?
                """,
                (int(pending_id), int(persona_user_id)),
            )
            if cursor.rowcount == 0:
                return None
            row_cursor = await conn.execute(
                """
                SELECT id, persona_user_id, telegram_chat_id, tool_name, args_json
                  FROM telegram_pending_action
                 WHERE id = ?
                """,
                (int(pending_id),),
            )
            row = await row_cursor.fetchone()
        if row is None:  # pragma: no cover - row just updated in this transaction
            return None
        return {
            "id": int(row["id"]),
            "persona_user_id": int(row["persona_user_id"]),
            "telegram_chat_id": int(row["telegram_chat_id"]),
            "tool_name": str(row["tool_name"]),
            "args": json.loads(row["args_json"]),
        }

    async def get(
        self,
        persona_user_id: int,
        pending_id: int,
    ) -> dict[str, Any] | None:
        """Read-only lookup, e.g. to render the confirmation card. Never used
        to decide whether an action may execute -- only ``claim`` may do that.
        """
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, persona_user_id, telegram_chat_id, tool_name, args_json
                  FROM telegram_pending_action
                 WHERE id = ? AND persona_user_id = ?
                """,
                (int(pending_id), int(persona_user_id)),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "persona_user_id": int(row["persona_user_id"]),
            "telegram_chat_id": int(row["telegram_chat_id"]),
            "tool_name": str(row["tool_name"]),
            "args": json.loads(row["args_json"]),
        }


def _clean(value: object, limit: int) -> str:
    text = "".join(
        char for char in str(value or "") if char >= " " and char != "\x7f"
    )
    return " ".join(text.split())[:limit]


__all__ = ["TTL_MINUTES", "PendingAction", "PendingActionStore"]
