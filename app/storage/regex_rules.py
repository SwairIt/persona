"""Regex auto-tag rules — define patterns that auto-tag screenshots when OCR completes.

Each rule pairs a regex `pattern` with a `tag_name`. After OCR finishes for a
screenshot, the worker calls :func:`apply_rules_to_ocr`, which tests the OCR
text against every enabled rule and attaches the matching tag(s).
"""

from __future__ import annotations

import re
from typing import Any

import aiosqlite

from app.logging_setup import get_logger
from app.storage.tags import create_tag, tag_screenshot

log = get_logger("persona.regex_rules")


async def list_rules(
    conn: aiosqlite.Connection,
    *,
    only_enabled: bool = False,
) -> list[dict[str, Any]]:
    """Return all regex auto-tag rules, newest first."""
    sql = (
        "SELECT id, pattern, tag_name, case_insensitive, enabled, "
        "created_at, last_matched_at, match_count "
        "FROM regex_auto_tag_rules"
    )
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id DESC"
    cursor = await conn.execute(sql)
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "pattern": str(row["pattern"]),
            "tag_name": str(row["tag_name"]),
            "case_insensitive": bool(row["case_insensitive"]),
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"]),
            "last_matched_at": row["last_matched_at"],
            "match_count": int(row["match_count"]),
        }
        for row in rows
    ]


async def create_rule(
    conn: aiosqlite.Connection,
    *,
    pattern: str,
    tag_name: str,
    case_insensitive: bool = True,
) -> int:
    """Create a new auto-tag rule. Validates the regex compiles."""
    pattern = (pattern or "").strip()
    tag_name = (tag_name or "").strip().lower()
    if not pattern:
        msg = "pattern is required"
        raise ValueError(msg)
    if not tag_name:
        msg = "tag_name is required"
        raise ValueError(msg)
    try:
        re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        msg = f"Invalid regex: {exc}"
        raise ValueError(msg) from exc

    cursor = await conn.execute(
        """
        INSERT INTO regex_auto_tag_rules (pattern, tag_name, case_insensitive, enabled)
        VALUES (?, ?, ?, 1)
        """,
        (pattern, tag_name, 1 if case_insensitive else 0),
    )
    await conn.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        msg = "INSERT did not return a row id"
        raise RuntimeError(msg)
    return int(row_id)


async def delete_rule(conn: aiosqlite.Connection, rule_id: int) -> None:
    """Delete an auto-tag rule by id."""
    await conn.execute(
        "DELETE FROM regex_auto_tag_rules WHERE id = ?",
        (int(rule_id),),
    )
    await conn.commit()


async def toggle_rule(
    conn: aiosqlite.Connection,
    rule_id: int,
    enabled: bool,
) -> None:
    """Enable or disable a rule without deleting it."""
    await conn.execute(
        "UPDATE regex_auto_tag_rules SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, int(rule_id)),
    )
    await conn.commit()


async def apply_rules_to_ocr(
    conn: aiosqlite.Connection,
    *,
    screenshot_id: int,
    ocr_text: str | None,
) -> list[str]:
    """Test each enabled rule against `ocr_text`; tag the screenshot on every match.

    Returns the list of tag names that were applied.
    """
    if not ocr_text:
        return []

    rules = await list_rules(conn, only_enabled=True)
    if not rules:
        return []

    applied: list[str] = []
    for rule in rules:
        try:
            flags = re.IGNORECASE if rule["case_insensitive"] else 0
            regex = re.compile(rule["pattern"], flags)
        except re.error as exc:
            log.warning(
                "regex_rules.compile_failed",
                rule_id=rule["id"],
                pattern=rule["pattern"],
                error=str(exc),
            )
            continue

        if regex.search(ocr_text) is None:
            continue

        try:
            tag_id = await create_tag(conn, name=rule["tag_name"])
            await tag_screenshot(conn, screenshot_id, tag_id)
            await conn.execute(
                "UPDATE regex_auto_tag_rules "
                "SET last_matched_at = datetime('now'), match_count = match_count + 1 "
                "WHERE id = ?",
                (int(rule["id"]),),
            )
            await conn.commit()
            applied.append(rule["tag_name"])
        except Exception as exc:
            log.warning(
                "regex_rules.apply_failed",
                rule_id=rule["id"],
                screenshot_id=screenshot_id,
                error=str(exc),
            )

    if applied:
        log.info(
            "regex_rules.applied",
            screenshot_id=screenshot_id,
            tags=applied,
        )
    return applied
