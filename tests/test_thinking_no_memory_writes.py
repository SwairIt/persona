"""Structural guarantee for Rule 1 (owner mandate 2026-07-30): the thinking
loop has NO write path into memory. Not a prompt instruction, not a promise
— a fact about the source code. This test greps the actual package source
rather than trusting a comment, because a prompt telling the model not to
write memory is not the same as the code being unable to.
"""

from __future__ import annotations

from pathlib import Path

import app.thinking as thinking_pkg

# Any of these appearing in app/thinking source would be a write path into
# long-term memory: direct SQL against user_memory, or a call into one of
# app.chat.user_memory's writer functions (reader functions like list_memory,
# search_memory and count_memory are fine — reading evidence is required by
# Rule 2, only writing back is forbidden).
_FORBIDDEN_SQL_WRITE_PATTERNS = (
    "insert into user_memory",
    "update user_memory",
    "delete from user_memory",
)
_FORBIDDEN_MEMORY_WRITERS = (
    "add_memory",
    "reconcile_and_add",
    "extract_and_store",
    "invalidate_memory",
    "edit_memory",
    "delete_memory",
    "restore_memory",
    "set_pinned",
    "consolidate_memories",
    "_add_memory_in_transaction",
    "_invalidate_memory_in_transaction",
)


def _thinking_source_files() -> list[Path]:
    pkg_dir = Path(thinking_pkg.__file__).parent
    return sorted(pkg_dir.rglob("*.py"))


def test_thinking_package_has_no_direct_sql_write_to_user_memory() -> None:
    offenders: list[str] = []
    for path in _thinking_source_files():
        lowered = path.read_text(encoding="utf-8").lower()
        for pattern in _FORBIDDEN_SQL_WRITE_PATTERNS:
            if pattern in lowered:
                offenders.append(f"{path}: {pattern!r}")
    assert offenders == [], (
        "app/thinking must have no write path to user_memory; found: " + "; ".join(offenders)
    )


def test_thinking_package_never_calls_a_user_memory_writer_function() -> None:
    offenders: list[str] = []
    for path in _thinking_source_files():
        text = path.read_text(encoding="utf-8")
        for name in _FORBIDDEN_MEMORY_WRITERS:
            if name in text:
                offenders.append(f"{path}: {name!r}")
    assert offenders == [], (
        "app/thinking must never call a user_memory writer; found: " + "; ".join(offenders)
    )
