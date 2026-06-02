"""OCR phrase-based auto-tagging — literal multi-word phrase rules.

Unlike :mod:`app.storage.regex_rules`, these rules match by exact substring via
:func:`str.find` — no regex engine, no metacharacters, just literal phrases like
``"daily standup"`` mapping to a tag such as ``standup``. Each rule has an
independent case-sensitivity flag.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr_phrase_tags")


async def list_rules() -> list[dict[str, Any]]:
    """Return every phrase-tag rule, newest first."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, phrase, tag, case_sensitive, created_at "
            "FROM ocr_phrase_tag ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "phrase": str(row["phrase"]),
            "tag": str(row["tag"]),
            "case_sensitive": bool(row["case_sensitive"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def add(phrase: str, tag: str, case_sensitive: bool = False) -> int:
    """Create a new phrase-tag rule. Returns the new row id.

    Phrase and tag are trimmed; tag is lowercased to match the rest of the
    tag pipeline. Duplicate (phrase, tag) pairs raise :class:`ValueError`.
    """
    cleaned_phrase = (phrase or "").strip()
    cleaned_tag = (tag or "").strip().lower()
    if not cleaned_phrase:
        msg = "phrase is required"
        raise ValueError(msg)
    if not cleaned_tag:
        msg = "tag is required"
        raise ValueError(msg)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM ocr_phrase_tag WHERE phrase = ? AND tag = ?",
            (cleaned_phrase, cleaned_tag),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            msg = "rule already exists for this phrase/tag pair"
            raise ValueError(msg)

        cursor = await conn.execute(
            "INSERT INTO ocr_phrase_tag (phrase, tag, case_sensitive) "
            "VALUES (?, ?, ?)",
            (cleaned_phrase, cleaned_tag, 1 if case_sensitive else 0),
        )
        await conn.commit()
        row_id = cursor.lastrowid
    if row_id is None:
        msg = "INSERT did not return a row id"
        raise RuntimeError(msg)
    return int(row_id)


async def delete(rule_id: int) -> None:
    """Remove the phrase-tag rule with the given id."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM ocr_phrase_tag WHERE id = ?",
            (int(rule_id),),
        )
        await conn.commit()


async def apply_phrase_rules(ocr_text: str) -> list[str]:
    """Return tag names whose phrases appear inside ``ocr_text``.

    Each rule's :data:`case_sensitive` flag is respected independently. Matching
    is performed with :func:`str.find` so no regex metacharacters are honoured —
    spaces, punctuation and digits are all literal. Duplicate tags (e.g. two
    phrases mapping to the same tag) are collapsed in the returned list.
    """
    if not ocr_text:
        return []

    rules = await list_rules()
    if not rules:
        return []

    lowered_text = ocr_text.lower()
    seen: set[str] = set()
    applied: list[str] = []
    for rule in rules:
        phrase = rule["phrase"]
        tag = rule["tag"]
        if rule["case_sensitive"]:
            found = ocr_text.find(phrase) != -1
        else:
            found = lowered_text.find(phrase.lower()) != -1
        if found and tag not in seen:
            seen.add(tag)
            applied.append(tag)
    return applied
