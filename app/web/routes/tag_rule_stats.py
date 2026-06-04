"""Per-rule auto-tagger stats — ``GET /api/tag-rules/stats.json``.

Exposes one JSON row per enabled-or-disabled regex auto-tag rule:

* ``rule_id`` — primary key in ``regex_auto_tag_rules``.
* ``pattern`` — the user-entered regex (echoed for the dashboard).
* ``tag_name`` — tag that the rule attaches on match.
* ``last_screenshot_id`` — the worker's watermark, i.e. the highest
  screenshot id inspected for this rule. ``0`` when the rule has never
  ticked yet.
* ``auto_tagged_count`` — number of rows in ``screenshot_tags`` whose
  ``tag_id`` resolves to ``rule.tag_name`` via the ``tags`` table.

The count is intentionally best-effort: the same tag can be applied by
the manual bulk-tag UI, by the per-shot OCR-time rule pass
(:mod:`app.storage.regex_rules`), and by this worker. The JSON includes
``auto_tagged_count_note`` to flag the ambiguity so the dashboard does
not present the number as "rule fired N times" — that would be a
deeper join through ``regex_auto_tag_rules.match_count`` or an audit
table, neither of which is in scope here.
"""

from __future__ import annotations

from typing import TypedDict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_rule_stats")

router = APIRouter(tags=["tag-rule-stats"])


_AUTO_TAGGED_COUNT_NOTE: str = (
    "Counts every screenshot_tags row whose tag matches the rule's "
    "tag_name — this includes manual tagging and the OCR-time rule "
    "pass, not only this worker's inserts."
)


class _RuleStatRow(TypedDict):
    """Shape of one entry inside the JSON ``rules`` list."""

    rule_id: int
    pattern: str
    tag_name: str
    last_screenshot_id: int
    auto_tagged_count: int


async def _load_rule_stats() -> list[_RuleStatRow]:
    """Read the rules + watermarks + counts in a single connection.

    We deliberately fetch everything we need in three small queries
    rather than one big JOIN — ``regex_auto_tag_rules`` is tiny (the
    user types these by hand) so the round-trip cost is negligible and
    the per-step SQL is far easier to read in the journal.
    """
    async with get_connection() as conn:
        rules_cursor = await conn.execute(
            "SELECT id, pattern, tag_name FROM regex_auto_tag_rules "
            "ORDER BY id ASC",
        )
        rule_rows = await rules_cursor.fetchall()
        if not rule_rows:
            return []

        watermarks: dict[int, int] = {}
        watermark_cursor = await conn.execute(
            "SELECT rule_id, last_screenshot_id FROM tag_rule_watermark",
        )
        for row in await watermark_cursor.fetchall():
            watermarks[int(row["rule_id"])] = int(row["last_screenshot_id"])

        # Pull the per-tag-name counts in one query so the route is
        # O(1) round-trips regardless of how many rules the user has.
        # The ``tags`` table is the single source of truth for the
        # name→id mapping; ``screenshot_tags`` only stores the id.
        tag_names = sorted({str(row["tag_name"]) for row in rule_rows})
        placeholders = ",".join("?" * len(tag_names))
        count_cursor = await conn.execute(
            f"SELECT t.name AS name, COUNT(st.screenshot_id) AS n "  # noqa: S608
            f"FROM tags t "
            f"LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
            f"WHERE t.name IN ({placeholders}) "
            f"GROUP BY t.name",
            tag_names,
        )
        counts_by_name: dict[str, int] = {}
        for row in await count_cursor.fetchall():
            counts_by_name[str(row["name"])] = int(row["n"])

    out: list[_RuleStatRow] = []
    for row in rule_rows:
        tag_name = str(row["tag_name"])
        rule_id = int(row["id"])
        out.append(
            _RuleStatRow(
                rule_id=rule_id,
                pattern=str(row["pattern"]),
                tag_name=tag_name,
                last_screenshot_id=watermarks.get(rule_id, 0),
                auto_tagged_count=counts_by_name.get(tag_name, 0),
            )
        )
    return out


@router.get("/api/tag-rules/stats.json", response_class=JSONResponse)
async def tag_rule_stats_json() -> JSONResponse:
    """Return the per-rule stats payload.

    The shape is::

        {
          "rules": [
            {"rule_id": 1, "pattern": "...", "tag_name": "...",
             "last_screenshot_id": 12345, "auto_tagged_count": 42},
            ...
          ],
          "auto_tagged_count_note": "..."
        }

    ``auto_tagged_count_note`` is always present so consumers can
    surface the caveat without parsing the docstring.
    """
    try:
        rules = await _load_rule_stats()
    except Exception as exc:
        log.exception("tag_rule_stats.query_failed", error=str(exc))
        return JSONResponse(
            {"rules": [], "auto_tagged_count_note": _AUTO_TAGGED_COUNT_NOTE},
            status_code=500,
        )

    payload: dict[str, object] = {
        "rules": list(rules),
        "auto_tagged_count_note": _AUTO_TAGGED_COUNT_NOTE,
    }
    return JSONResponse(payload)


__all__ = ["router"]
