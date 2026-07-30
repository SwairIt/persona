"""Real evidence for the thinking loop — no evidence, no thought.

The thinking loop used to ask the model things like "what did you learn
about the owner?" with nothing to go on, so the model invented answers.
This module is the fix: it gathers the ONLY two things the loop is allowed
to treat as ground truth about the owner —

  * the owner's own recent chat messages (read-only query, mirrors the
    gathering ``app.chat.reflection._gather_documents`` already does), and
  * existing curated facts from ``user_memory`` (read-only, via
    :mod:`app.chat.user_memory`'s public read API).

— and renders them into one bounded text block, newest first, for the
prompt to quote from. It is read-only: nothing in this module, or anywhere
under :mod:`app.thinking`, writes to ``user_memory`` or any other memory
table. See ``tests/test_thinking_no_memory_writes.py`` for the structural
guarantee.

``gather_evidence`` returns ``""`` when there is nothing real to show, and
callers (``app.thinking.loop.seed_chain``) treat that as a hard stop for
evidence-dependent seed kinds rather than falling back to guessing.
"""

from __future__ import annotations

from typing import Any

_MAX_CHARS = 4000
_MAX_MESSAGES = 40
_MAX_MESSAGE_CHARS = 300
_MAX_MEMORY_ITEMS = 20
_TELEGRAM_GROUP_PREFIX = "[Telegram · "


def _is_untrusted_group_message(text: str) -> bool:
    """Mirror ``app.chat.reflection._is_untrusted_group_message``.

    Telegram group speech is prefixed this way; it must never be presented
    to the thinking loop as something the owner said.
    """
    return (text or "").lstrip().startswith(_TELEGRAM_GROUP_PREFIX)


async def _recent_owner_messages(persona_user_id: int) -> str:
    """Owner's own chat messages, newest first, read-only."""
    from app.storage.db import get_connection  # noqa: PLC0415

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT m.content, m.created_at "
                "FROM chat_message m JOIN chat_session s ON s.id = m.session_id "
                "WHERE s.user_id = ? AND m.role = 'user' "
                "ORDER BY m.id DESC LIMIT ?",
                (int(persona_user_id), _MAX_MESSAGES),
            )
            rows = await cursor.fetchall()
    except Exception:  # noqa: BLE001 — no chat history yet / table missing
        return ""
    lines: list[str] = []
    for row in rows:
        raw = str(row["content"] or "")
        if _is_untrusted_group_message(raw):
            continue
        text = " ".join(raw.split())
        if not text:
            continue
        lines.append(f"• {text[:_MAX_MESSAGE_CHARS]}")
    return "\n".join(lines)


async def _known_facts(persona_user_id: int) -> str:
    """Existing curated ``user_memory`` facts, pinned/salient first."""
    from app.chat.user_memory import list_memory  # noqa: PLC0415

    try:
        items = await list_memory(
            persona_user_id, limit=_MAX_MEMORY_ITEMS, order_by_salience=True
        )
    except Exception:  # noqa: BLE001 — no memory yet / table missing
        return ""
    lines = [f"• {item['text']}" for item in items if str(item.get("text") or "").strip()]
    return "\n".join(lines)


async def gather_evidence(persona_user_id: int) -> str:
    """Collect the bounded evidence block the thinking loop may quote from.

    Returns ``""`` when there is nothing real — no owner messages and no
    stored facts — which is the signal ``seed_chain`` uses to refuse to
    seed an evidence-dependent chain rather than let the model invent.
    """
    sections: list[str] = []
    messages = await _recent_owner_messages(persona_user_id)
    if messages:
        sections.append("Недавние сообщения владельца (только это — не выдумывай):\n" + messages)
    facts = await _known_facts(persona_user_id)
    if facts:
        sections.append("Уже известные факты о владельце:\n" + facts)
    return "\n\n".join(sections)[:_MAX_CHARS]


__all__ = ["gather_evidence"]
