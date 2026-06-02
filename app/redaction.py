"""Regex-based OCR text redaction.

Database-backed rules let the user mask sensitive substrings (emails,
credit cards, bearer tokens, …) in the searchable OCR text *before* it is
written to the screenshots table or indexed by FTS5. The original image
stays untouched — only the text representation is sanitised, so leaked
secrets never surface in search results or digests.

Each match is replaced with a fixed mask ``***``. The replacement is
applied per enabled rule, in stable PRIMARY KEY order, so the masked
output is deterministic.

Patterns that fail to compile are skipped (and logged) rather than
crashing the worker — a single bad user-supplied regex must never block
the OCR pipeline.
"""

from __future__ import annotations

import re
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.redaction")

MASK = "***"


async def list_rules() -> list[dict[str, Any]]:
    """Return every redaction rule, oldest first (insertion order)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name, pattern, enabled, created_at "
            "FROM redaction_rule "
            "ORDER BY created_at, name"
        )
        rows = await cursor.fetchall()
    return [
        {
            "name": str(row["name"]),
            "pattern": str(row["pattern"]),
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def _list_enabled_rules() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name, pattern FROM redaction_rule "
            "WHERE enabled = 1 ORDER BY created_at, name"
        )
        rows = await cursor.fetchall()
    return [
        {"name": str(row["name"]), "pattern": str(row["pattern"])}
        for row in rows
    ]


async def apply_redaction(text: str) -> tuple[str, int]:
    """Mask every enabled rule match in ``text`` with ``***``.

    Returns a ``(cleaned_text, masks_applied)`` tuple. Patterns are
    compiled once per call. Invalid regex are skipped with a warning so
    one bad user rule cannot derail OCR.
    """
    if not text:
        return text, 0

    rules = await _list_enabled_rules()
    if not rules:
        return text, 0

    cleaned = text
    masks_applied = 0
    for rule in rules:
        pattern = rule["pattern"]
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            log.warning(
                "redaction.bad_pattern",
                name=rule["name"],
                pattern=pattern,
                error=str(exc),
            )
            continue

        cleaned, count = regex.subn(MASK, cleaned)
        masks_applied += count

    return cleaned, masks_applied


async def create_rule(*, name: str, pattern: str) -> None:
    """Create a new redaction rule. Validates the regex compiles."""
    name = (name or "").strip()
    pattern = (pattern or "").strip()
    if not name:
        msg = "name is required"
        raise ValueError(msg)
    if not pattern:
        msg = "pattern is required"
        raise ValueError(msg)
    try:
        re.compile(pattern)
    except re.error as exc:
        msg = f"Invalid regex: {exc}"
        raise ValueError(msg) from exc

    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO redaction_rule (name, pattern, enabled) "
            "VALUES (?, ?, 1)",
            (name, pattern),
        )
        await conn.commit()


async def toggle_rule(name: str) -> None:
    """Flip the ``enabled`` flag of a rule. Idempotent if missing."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE redaction_rule SET enabled = 1 - enabled WHERE name = ?",
            (name,),
        )
        await conn.commit()


async def delete_rule(name: str) -> None:
    """Delete a rule by name. Idempotent if missing."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM redaction_rule WHERE name = ?",
            (name,),
        )
        await conn.commit()
