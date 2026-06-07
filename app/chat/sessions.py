"""CRUD + LLM-history shaping for the persistent chat threads."""

from __future__ import annotations

from typing import Any, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.chat")

# Cap on stored content per message. Anything bigger is almost certainly
# a paste from a code review or a runaway model output — refuse to write
# rather than bloat the table.
_MAX_CONTENT_BYTES = 32 * 1024

# T20 (2026-06-07): bumped from 20 → 50 because the user said
# "you promised to save EVERYTHING". The DB ALWAYS stored every message
# (chat_message is append-only). The 20-cap was only on what gets
# replayed to the model per turn. 50 turns ≈ ~10k tokens of history
# which still fits comfortably in Qwen 3B's 32k window after the system
# prompt and user input. For sessions older than 50 turns the route
# layer can later inject a summary of the omitted prefix — for now we
# just show recent context, which covers 99% of practical chats.
_DEFAULT_HISTORY_TURNS = 50


class ChatSession(TypedDict):
    id: int
    user_id: int
    title: str
    provider: str | None
    model: str | None
    created_at: str
    updated_at: str


class ChatMessage(TypedDict):
    id: int
    session_id: int
    role: str
    content: str
    model_used: str | None
    created_at: str


def _row_to_session(row: Any) -> ChatSession:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "title": str(row["title"]),
        "provider": str(row["provider"]) if row["provider"] is not None else None,
        "model": str(row["model"]) if row["model"] is not None else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _row_to_message(row: Any) -> ChatMessage:
    return {
        "id": int(row["id"]),
        "session_id": int(row["session_id"]),
        "role": str(row["role"]),
        "content": str(row["content"]),
        "model_used": (
            str(row["model_used"]) if row["model_used"] is not None else None
        ),
        "created_at": str(row["created_at"]),
    }


def _derive_title(first_user_message: str) -> str:
    """Pick a short title from the user's first message.

    Strip whitespace, cut to 60 chars, no newlines. We keep this purely
    deterministic — calling an LLM just to summarise the title would be
    fancy but wastes a real query on a UI nicety.
    """
    cleaned = " ".join((first_user_message or "").split()).strip()
    if not cleaned:
        return "Без названия"
    return cleaned[:60]


async def create_session(
    user_id: int,
    title: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> ChatSession:
    """Open a new chat thread. Title can come later via ``rename_session``."""
    chosen_title = (title or "").strip() or "Без названия"
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO chat_session (user_id, title, provider, model) "
            "VALUES (?, ?, ?, ?)",
            (user_id, chosen_title[:120], provider, model),
        )
        await conn.commit()
        session_id = cursor.lastrowid or 0
        cursor = await conn.execute(
            "SELECT * FROM chat_session WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("chat_session insert reported success but lookup failed")
    log.info("chat.session.created", session_id=session_id, user_id=user_id)
    return _row_to_session(row)


async def list_sessions(user_id: int, limit: int = 50) -> list[ChatSession]:
    """Recent-first sidebar listing."""
    safe_limit = max(1, min(500, int(limit)))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM chat_session "
            "WHERE user_id = ? "
            "ORDER BY updated_at DESC "
            "LIMIT ?",
            (user_id, safe_limit),
        )
        rows = await cursor.fetchall()
    return [_row_to_session(r) for r in rows]


async def get_session(user_id: int, session_id: int) -> ChatSession | None:
    """Return one session scoped to its owner. ``None`` for not-found / wrong-user."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM chat_session WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        row = await cursor.fetchone()
    return _row_to_session(row) if row is not None else None


async def list_messages(session_id: int, limit: int = 500) -> list[ChatMessage]:
    """Chronological history of one session. Use the helper-shaped
    :func:`build_history_for_llm` when piping into the model."""
    safe_limit = max(1, min(2000, int(limit)))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM chat_message "
            "WHERE session_id = ? "
            "ORDER BY id ASC "
            "LIMIT ?",
            (session_id, safe_limit),
        )
        rows = await cursor.fetchall()
    return [_row_to_message(r) for r in rows]


async def append_message(
    session_id: int,
    role: str,
    content: str,
    model_used: str | None = None,
) -> ChatMessage:
    """Log one turn. Updates ``chat_session.updated_at`` so the sidebar
    keeps the thread on top."""
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"unknown role: {role!r}")
    text = (content or "").strip()
    if not text:
        raise ValueError("content must not be empty")
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        text = encoded[:_MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO chat_message (session_id, role, content, model_used) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, text, model_used),
        )
        await conn.commit()
        new_id = cursor.lastrowid or 0
        await conn.execute(
            "UPDATE chat_session SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        await conn.commit()
        cursor = await conn.execute(
            "SELECT * FROM chat_message WHERE id = ?", (new_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("chat_message insert reported success but lookup failed")
    return _row_to_message(row)


async def rename_session(user_id: int, session_id: int, title: str) -> bool:
    """Change the sidebar label. Returns False when the row doesn't belong
    to the caller."""
    clean = (title or "").strip()
    if not clean:
        return False
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE chat_session SET title = ?, updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (clean[:120], session_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def update_session_model(
    user_id: int,
    session_id: int,
    provider: str | None,
    model: str | None,
) -> bool:
    """Pin a provider+model to this session. Used when the user switches
    models mid-conversation — subsequent /ask turns in the same thread
    use the new settings, history stays untouched."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE chat_session SET provider = ?, model = ?, "
            "                        updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (provider, model, session_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def delete_session(user_id: int, session_id: int) -> bool:
    """Wipe one thread. ``ON DELETE CASCADE`` clears the message rows."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM chat_session WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def touch_session(user_id: int, session_id: int) -> None:
    """Update ``updated_at`` without changing anything else — used when
    a derived title (from first user message) gets assigned."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT title FROM chat_session "
            "WHERE id = ? AND user_id = ? AND title = 'Без названия' LIMIT 1",
            (session_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        cursor = await conn.execute(
            "SELECT content FROM chat_message "
            "WHERE session_id = ? AND role = 'user' "
            "ORDER BY id ASC LIMIT 1",
            (session_id,),
        )
        first = await cursor.fetchone()
        if first is None:
            return
        await conn.execute(
            "UPDATE chat_session SET title = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (_derive_title(str(first["content"]))[:120], session_id),
        )
        await conn.commit()


async def build_history_for_llm(
    session_id: int, max_turns: int = _DEFAULT_HISTORY_TURNS
) -> list[dict[str, str]]:
    """Return the last ``max_turns`` exchanges as OpenAI-compatible
    ``[{"role": ..., "content": ...}, ...]``.

    The LLM provider sees this in addition to whatever fresh user
    message + system prompt the caller assembles. Each provider class in
    ``app/llm/client.py`` already accepts a single ``request.user`` and
    ``request.system`` — for now we surface chat history by inlining it
    INTO ``request.user`` (because changing the client protocol to take
    arbitrary message lists would ripple through every call site). This
    function builds the right shape so a future refactor can plug it in
    directly without rebuilding the SQL.
    """
    safe_n = max(1, min(200, int(max_turns)))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT role, content FROM chat_message "
            "WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, safe_n * 2),  # × 2 for user+assistant pairs
        )
        rows = await cursor.fetchall()
    # Reverse so the returned list is oldest-first (LLM-friendly order).
    return [
        {"role": str(r["role"]), "content": str(r["content"])}
        for r in reversed(rows)
    ]
