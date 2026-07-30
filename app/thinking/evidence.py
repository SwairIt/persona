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


async def _owner_display_name(persona_user_id: int) -> str:
    """The owner's resolved display name, read-only, or ``""`` if unknown.

    Mirrors the precedence ``TelegramPeopleRepository.identity_context``
    already applies: an owner-authored override name (set on
    ``/settings/telegram-people``) wins over the raw Telegram
    ``display_name``. Resolved via the repository rather than any hardcoded
    id — this module must keep working for whichever ``telegram_user_id``
    is actually marked ``is_owner`` for this tenant.
    """
    from app.integrations.telegram.people import TelegramPeopleRepository  # noqa: PLC0415
    from app.storage.db import get_connection  # noqa: PLC0415

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT telegram_user_id, display_name FROM telegram_person "
                "WHERE persona_user_id=? AND is_owner=1 LIMIT 1",
                (int(persona_user_id),),
            )
            row = await cursor.fetchone()
    except Exception:  # noqa: BLE001 — no Telegram identity yet / table missing
        return ""
    if row is None:
        return ""
    telegram_user_id = int(row["telegram_user_id"])
    override = None
    try:
        override = await TelegramPeopleRepository().get_override(
            persona_user_id, telegram_user_id
        )
    except Exception:  # noqa: BLE001 — override lookup is best-effort
        override = None
    name_override = str((override or {}).get("display_name_override") or "").strip()
    if name_override:
        return name_override
    return str(row["display_name"] or "").strip()


async def _owner_identity_line(persona_user_id: int) -> str:
    """State plainly who the owner is, the same authoritative way
    ``TelegramPeopleRepository.identity_context`` already does for Telegram
    turns — so the thinking loop can never again mistake the owner's own
    name, mentioned inside the evidence, for a third party.
    """
    name = await _owner_display_name(persona_user_id)
    if name:
        return (
            "КТО ВЛАДЕЛЕЦ: владельца зовут "
            f"{name}. Это единственный человек, о котором ниже реальные "
            "данные, и тот, для кого ты, Persona, думаешь. Если в тексте "
            f"ниже он упомянут по имени «{name}» — это тот же самый "
            "владелец, а не третье лицо и не кто-то ещё."
        )
    return (
        "КТО ВЛАДЕЛЕЦ: имя владельца пока не определено, зови его "
        "«владелец». Это единственный человек, о котором ниже реальные "
        "данные, и тот, для кого ты, Persona, думаешь. Если где-то в тексте "
        "ниже он всё же упомянут по имени — это тот же самый владелец, "
        "а не третье лицо и не кто-то ещё."
    )


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
    if not sections:
        return ""
    identity = await _owner_identity_line(persona_user_id)
    return (identity + "\n\n" + "\n\n".join(sections))[:_MAX_CHARS]


__all__ = ["gather_evidence"]
