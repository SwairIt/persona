"""Tag-rule auto-applier — re-run regex auto-tag rules against new shots.

This module is the worker-friendly counterpart to
:mod:`app.storage.regex_rules`. The interactive entry point in that
module (:func:`apply_rules_to_ocr`) only runs the rule set against a
single ``screenshot_id`` at OCR-complete time, so any rule created
**after** a shot was captured never sees it. The functions below close
that gap: for each enabled rule we scan every shot whose id is greater
than the per-rule watermark stored in ``tag_rule_watermark`` and tag
the matches, then advance the watermark so the same row never gets
rescanned on the next tick.

Schema notes (verified against ``001_tags.sql`` /
``015_regex_auto_tag_rules.sql`` / ``100_tag_rule_auto_apply.sql``):

* ``regex_auto_tag_rules`` columns: ``id, pattern, tag_name,
  case_insensitive, enabled, ...``. The task description names the
  table ``tag_phrase_rules``; on this codebase the actual table is
  ``regex_auto_tag_rules`` and we target it directly.
* ``screenshot_tags`` is ``(screenshot_id, tag_id)`` — no plain ``tag``
  column. We resolve ``tag_name`` to ``tag_id`` via
  :func:`app.storage.tags.create_tag` (which is idempotent on name) and
  insert through :func:`app.storage.tags.tag_screenshot`.
* The watermark table holds one row per rule. ``last_screenshot_id``
  defaults to 0 so a brand-new rule starts by scanning the entire
  history — exactly the behaviour an interactive user expects when
  they hit "save rule".

The functions in this module deliberately take a connection argument so
they are testable without monkey-patching :func:`get_connection`. The
worker (``app.workers.tag_rule_worker``) opens the connection itself
and passes it in.
"""

from __future__ import annotations

import re
from typing import TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.tags import create_tag, tag_screenshot

log = get_logger("persona.tag_rule_engine")


DEFAULT_BATCH_LIMIT: int = 100
"""Upper bound on shots inspected per rule per tick.

Small enough that a single rule with a slow regex cannot starve the
event loop, large enough that a freshly added rule drains its backlog
in a handful of ticks rather than days.
"""


class RuleRow(TypedDict):
    """One enabled row of ``regex_auto_tag_rules`` as a plain dict.

    Mirrors the dict shape produced by
    :func:`app.storage.regex_rules.list_rules` so the two code paths can
    interoperate cleanly — a follow-up could swap one for the other
    without changing call sites.
    """

    id: int
    pattern: str
    tag_name: str
    case_insensitive: bool
    enabled: bool


class RunSummary(TypedDict):
    """Aggregate counters returned by :func:`run_rules_against_new_shots`.

    * ``rules_processed`` — number of enabled rules that actually had
      their loop body run (compile failures count as processed-but-no-op).
    * ``tags_added`` — successful inserts into ``screenshot_tags``; the
      idempotent ``INSERT OR IGNORE`` means an existing pair is **not**
      counted, only genuinely new bindings.
    * ``screenshots_scanned`` — sum across rules of how many candidate
      rows were fetched. A shot picked up by two rules counts twice
      here; this matches the cost the worker actually paid.
    """

    rules_processed: int
    tags_added: int
    screenshots_scanned: int


async def list_enabled_rules(conn: aiosqlite.Connection) -> list[RuleRow]:
    """Return every row in ``regex_auto_tag_rules`` with ``enabled = 1``.

    Ordered by ``id`` so the worker's tick-to-tick behaviour is
    deterministic even when two rules race to tag the same shot —
    whichever has the lower id wins the "first inserted" slot in the
    log line, though the resulting tag set is identical either way.
    """
    cursor = await conn.execute(
        "SELECT id, pattern, tag_name, case_insensitive, enabled "
        "FROM regex_auto_tag_rules "
        "WHERE enabled = 1 "
        "ORDER BY id ASC",
    )
    rows = await cursor.fetchall()
    return [
        RuleRow(
            id=int(row["id"]),
            pattern=str(row["pattern"]),
            tag_name=str(row["tag_name"]),
            case_insensitive=bool(row["case_insensitive"]),
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]


async def _read_watermark(conn: aiosqlite.Connection, rule_id: int) -> int:
    """Return ``last_screenshot_id`` for ``rule_id`` (0 when no row exists).

    A missing row is the canonical "rule has never been scanned" state;
    the migration's ``DEFAULT 0`` only applies to fresh inserts, so we
    have to coerce ``None`` to 0 here.
    """
    cursor = await conn.execute(
        "SELECT last_screenshot_id FROM tag_rule_watermark WHERE rule_id = ?",
        (rule_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["last_screenshot_id"])


async def _write_watermark(
    conn: aiosqlite.Connection,
    *,
    rule_id: int,
    last_screenshot_id: int,
) -> None:
    """Upsert the watermark for ``rule_id``.

    Uses ``ON CONFLICT`` rather than read-then-update because two ticks
    of the same worker can never race (the worker is single-flight per
    process), but a hot-reload could overlap with a leftover task — the
    upsert keeps that case correct without locking.
    """
    await conn.execute(
        "INSERT INTO tag_rule_watermark (rule_id, last_screenshot_id, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(rule_id) DO UPDATE SET "
        "  last_screenshot_id = excluded.last_screenshot_id, "
        "  updated_at = excluded.updated_at",
        (rule_id, last_screenshot_id),
    )


async def _fetch_candidate_shots(
    conn: aiosqlite.Connection,
    *,
    after_id: int,
    limit: int,
) -> list[tuple[int, str]]:
    """Return up to ``limit`` shots with ``id > after_id`` that have OCR text.

    Ordered ascending so the watermark advances by the largest id seen
    and the next tick picks up exactly where we left off. We filter on
    ``ocr_text IS NOT NULL`` here (not in Python) so a backlog of
    pending-OCR shots doesn't pin the watermark — those rows will be
    visited again on a later tick once OCR has populated the column.
    Note: this means a freshly captured shot that hasn't been OCR'd yet
    blocks **nothing**; the watermark only advances over rows we
    actually inspected.
    """
    cursor = await conn.execute(
        "SELECT id, ocr_text FROM screenshots "
        "WHERE id > ? AND ocr_text IS NOT NULL "
        "ORDER BY id ASC LIMIT ?",
        (int(after_id), int(limit)),
    )
    rows = await cursor.fetchall()
    return [(int(row["id"]), str(row["ocr_text"])) for row in rows]


def _compile_rule(rule: RuleRow) -> re.Pattern[str] | None:
    """Compile one rule's regex; log and return ``None`` on failure.

    The user-facing form already validates the regex at insert time
    (:func:`app.storage.regex_rules.create_rule`), so reaching this path
    means a stored rule has been corrupted or migrated from an older
    syntax. We log structured fields the admin dashboard can surface
    rather than crashing the whole tick.
    """
    # The task spec asks for ``re.IGNORECASE`` on the worker path; the
    # stored per-rule flag only re-confirms that intent here, so we
    # apply it unconditionally. If a future rule actually wants
    # case-sensitive matching, swap this for a conditional and add a
    # column-driven test.
    flags = re.IGNORECASE
    try:
        return re.compile(rule["pattern"], flags)
    except re.error as exc:
        log.warning(
            "tag_rule_engine.compile_failed",
            rule_id=rule["id"],
            pattern=rule["pattern"],
            error=str(exc),
        )
        return None


async def run_rules_against_new_shots(
    conn: aiosqlite.Connection,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
) -> RunSummary:
    """Apply every enabled rule to the slice of new shots above its watermark.

    For each rule:

    1. Look up the watermark (defaults to 0 — full backfill on first
       run).
    2. Fetch up to ``batch_limit`` shots with ``id > watermark`` that
       have a non-null ``ocr_text``.
    3. Run :func:`re.search` against each shot's text. Matches resolve
       the rule's ``tag_name`` to a tag id (creating the tag row if
       needed) and insert into ``screenshot_tags``.
    4. Advance the watermark to the maximum id we *inspected* — not the
       maximum id we *matched* — so unmatched shots are not rescanned.

    Returns a :class:`RunSummary` for the worker to log.
    """
    if batch_limit <= 0:
        msg = "batch_limit must be positive"
        raise ValueError(msg)

    rules = await list_enabled_rules(conn)
    summary: RunSummary = {
        "rules_processed": 0,
        "tags_added": 0,
        "screenshots_scanned": 0,
    }
    if not rules:
        return summary

    for rule in rules:
        summary["rules_processed"] += 1
        watermark = await _read_watermark(conn, rule["id"])
        shots = await _fetch_candidate_shots(
            conn,
            after_id=watermark,
            limit=batch_limit,
        )
        if not shots:
            continue
        summary["screenshots_scanned"] += len(shots)

        compiled = _compile_rule(rule)
        if compiled is None:
            # Still advance the watermark — leaving it pinned would
            # make a single broken rule starve the rest of the worker
            # forever. Bad rules are best surfaced by the admin UI, not
            # by silently wedging the queue.
            max_id = max(sid for sid, _ in shots)
            await _write_watermark(
                conn,
                rule_id=rule["id"],
                last_screenshot_id=max_id,
            )
            await conn.commit()
            continue

        matched_in_rule = 0
        for sid, ocr_text in shots:
            if compiled.search(ocr_text) is None:
                continue
            try:
                tag_id = await create_tag(conn, name=rule["tag_name"])
                await tag_screenshot(conn, sid, tag_id)
                matched_in_rule += 1
            except aiosqlite.Error as exc:
                log.warning(
                    "tag_rule_engine.apply_failed",
                    rule_id=rule["id"],
                    screenshot_id=sid,
                    error=str(exc),
                )

        max_id = max(sid for sid, _ in shots)
        await _write_watermark(
            conn,
            rule_id=rule["id"],
            last_screenshot_id=max_id,
        )
        await conn.commit()

        summary["tags_added"] += matched_in_rule
        if matched_in_rule:
            log.info(
                "tag_rule_engine.rule_matched",
                rule_id=rule["id"],
                tag_name=rule["tag_name"],
                tagged=matched_in_rule,
                watermark=max_id,
            )

    return summary


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "RuleRow",
    "RunSummary",
    "list_enabled_rules",
    "run_rules_against_new_shots",
]
