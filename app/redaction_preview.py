"""Interactive preview for OCR redaction rules.

Lets the operator paste arbitrary text and instantly see what the
currently enabled rules would strip — without polluting the real OCR
screenshots table or the search index. Same compile-and-skip semantics
as :mod:`app.redaction`: a single bad user-supplied regex is logged
once and skipped so the preview never 500s.
"""

from __future__ import annotations

import re
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.redaction_preview")

# Hard cap on the sample size we accept. Keeps a runaway paste from
# pinning a thread on a pathological regex (catastrophic backtracking)
# and bounds the JSON we ship back to the browser. The cap is generous
# enough for a normal OCR-text-sized paste — Persona screenshots rarely
# yield more than a few kB of text per frame.
MAX_SAMPLE_CHARS = 8000

# Mirrors :data:`app.redaction.MASK` so the preview matches the real
# pipeline character-for-character. We do not import the constant from
# :mod:`app.redaction` only to avoid coupling the two modules at import
# time; if the canonical mask ever changes, update both.
_MASK = "***"

# Cap on the per-rule examples we surface. Three short snippets are
# enough for the user to recognise what the pattern is catching without
# turning the response into a wall of matched text.
_EXAMPLES_PER_RULE = 3


async def _list_enabled_rules() -> list[dict[str, Any]]:
    """Return the enabled rules, oldest first.

    Mirrors :func:`app.redaction._list_enabled_rules` (private over
    there) so the preview is guaranteed to apply rules in the same
    deterministic order the production OCR redactor uses. Schema
    confirmed against ``app/storage/migrations/020_redaction_rules.sql``:
    columns are ``name TEXT PRIMARY KEY``, ``pattern TEXT NOT NULL``,
    ``enabled INTEGER``, ``created_at TEXT``.
    """
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


async def preview_redactions(sample_text: str) -> dict[str, Any]:
    """Run every enabled redaction rule against ``sample_text``.

    Returns a dict with the original text (clipped to
    :data:`MAX_SAMPLE_CHARS`), the masked output, and a per-rule
    breakdown of match counts plus up to :data:`_EXAMPLES_PER_RULE`
    sample matches so the user can see *what* each rule is catching.

    Matches are collected against the *original* text so the spans are
    stable regardless of how earlier rules rewrote the buffer. The
    masked output is produced by chaining :func:`re.sub` in the same
    rule order the OCR pipeline uses, so the preview is faithful even
    when two rules overlap.
    """
    # Cap up front so neither the regex engine nor the JSON wire ever
    # sees more than ``MAX_SAMPLE_CHARS``. The route layer already
    # enforces ``str`` via :func:`_coerce_sample_text` so we trust the
    # type annotation here and skip a defensive ``is None`` check that
    # mypy flags as unreachable.
    if len(sample_text) > MAX_SAMPLE_CHARS:
        sample_text = sample_text[:MAX_SAMPLE_CHARS]

    rules = await _list_enabled_rules()
    matches_summary: list[dict[str, Any]] = []
    cleaned = sample_text

    for rule in rules:
        name = rule["name"]
        pattern = rule["pattern"]
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            log.warning(
                "redaction_preview.bad_pattern",
                name=name,
                pattern=pattern,
                error=str(exc),
            )
            matches_summary.append(
                {
                    "rule_name": name,
                    "pattern": pattern,
                    "count": 0,
                    "examples": [],
                    "error": str(exc),
                }
            )
            continue

        # Collect spans + example fragments against the *original*
        # sample_text so positions and snippets reflect what the user
        # actually pasted, not the partially-masked intermediate state.
        spans: list[dict[str, Any]] = []
        for match in regex.finditer(sample_text):
            start, end = match.span()
            spans.append(
                {
                    "start": start,
                    "end": end,
                    "rule_name": name,
                }
            )

        examples = [sample_text[s["start"] : s["end"]] for s in spans[:_EXAMPLES_PER_RULE]]

        # Apply the substitution to ``cleaned`` (chained from the
        # previous rules) so the final redacted text mirrors the real
        # OCR pipeline output character-for-character.
        cleaned = regex.sub(_MASK, cleaned)

        matches_summary.append(
            {
                "rule_name": name,
                "pattern": pattern,
                "count": len(spans),
                "examples": examples,
            }
        )

    log.info(
        "redaction_preview.run",
        rules=len(rules),
        sample_len=len(sample_text),
        total_matches=sum(int(m["count"]) for m in matches_summary),
    )

    return {
        "original": sample_text,
        "redacted": cleaned,
        "matches": matches_summary,
    }
