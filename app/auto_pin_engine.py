"""Auto-pin engine — pin screenshots whose OCR matches a stored regex.

Sibling of :mod:`app.tag_rule_engine`. Where the tag-rule worker
attaches a tag to matching shots, this module flips
``screenshots.tier`` to ``'pinned'`` so the retention worker treats
them as user-curated and never demotes them.

Defensive design — a runaway regex (``.``, ``\\w+``, empty alternation)
would otherwise pin every shot ever captured. We cap successful pins
at ``daily_cap`` per UTC day across all rules; once the cap is hit the
remaining matches are *skipped silently* but the watermark still
advances so the loop drains. The cap counter lives in a day-stamped
``kv_settings`` row (:data:`DAILY_COUNTER_KV_KEY`) because the pinned
enum carries no timestamp to count from — see
:func:`_count_pinned_today` for what that costs.

Functions:

* :func:`list_enabled_rules` — every row in ``auto_pin_rule`` with
  ``enabled = 1``, ordered by id ASC for determinism.
* :func:`run_auto_pins` — one tick: walk rules, fetch shots above each
  watermark, ``re.search`` with ``IGNORECASE``, pin matches up to the
  daily cap, advance the watermark to the largest id inspected.

The caller (the worker) owns the connection so the engine is testable
without monkey-patching :func:`app.storage.db.get_connection`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.repository import get_kv, set_kv

log = get_logger("persona.auto_pin_engine")


DEFAULT_BATCH_LIMIT: int = 100
"""Upper bound on shots inspected per rule per tick.

Matches :data:`app.tag_rule_engine.DEFAULT_BATCH_LIMIT` — keeps a
single rule with a slow regex from starving the event loop while still
draining a freshly added rule's backlog in a handful of ticks.
"""


DEFAULT_DAILY_CAP: int = 20
"""Default cap on auto-pins per UTC day. Kept conservative on purpose."""


class AutoPinRuleRow(TypedDict):
    """One enabled row of ``auto_pin_rule`` as a plain dict."""

    id: int
    pattern: str
    enabled: bool
    description: str | None
    created_at: str


class AutoPinRunSummary(TypedDict):
    """Aggregate counters returned by :func:`run_auto_pins`.

    * ``rules_processed`` — every enabled rule whose loop body actually
      executed (a compile failure still counts: the watermark advances
      so a broken rule cannot starve the rest of the queue).
    * ``shots_pinned`` — number of rows we flipped to ``tier = 'pinned'``
      in this tick. Excludes shots already pinned before the tick started.
    * ``daily_cap_hit`` — ``True`` when at least one would-be pin was
      skipped because the daily cap was reached. Surfaced to the worker
      so the admin log line is informative.
    """

    rules_processed: int
    shots_pinned: int
    daily_cap_hit: bool


async def list_enabled_rules(conn: aiosqlite.Connection) -> list[AutoPinRuleRow]:
    """Return every ``auto_pin_rule`` row with ``enabled = 1``.

    Ordered by ``id`` so the worker's tick-to-tick behaviour is
    deterministic even when two rules race for the same shot — the
    lower-id rule wins the "first pinner" slot, though the resulting
    pinned set is identical either way.
    """
    cursor = await conn.execute(
        "SELECT id, pattern, enabled, description, created_at "
        "FROM auto_pin_rule "
        "WHERE enabled = 1 "
        "ORDER BY id ASC",
    )
    rows = await cursor.fetchall()
    return [
        AutoPinRuleRow(
            id=int(row["id"]),
            pattern=str(row["pattern"]),
            enabled=bool(row["enabled"]),
            description=(
                str(row["description"]) if row["description"] is not None else None
            ),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


async def _read_watermark(conn: aiosqlite.Connection, rule_id: int) -> int:
    """Return ``last_screenshot_id`` for ``rule_id`` — 0 when no row exists.

    Missing row is the canonical "rule has never been scanned" state;
    the migration's ``DEFAULT 0`` only applies to fresh inserts so we
    have to coerce ``None`` to 0 here.
    """
    cursor = await conn.execute(
        "SELECT last_screenshot_id FROM auto_pin_watermark WHERE rule_id = ?",
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

    ``ON CONFLICT`` rather than read-then-update so a leftover task
    from a hot-reload can't race the new worker — the upsert is
    correct without locking.
    """
    await conn.execute(
        "INSERT INTO auto_pin_watermark (rule_id, last_screenshot_id, updated_at) "
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

    Filters on ``ocr_text IS NOT NULL`` in SQL so a backlog of
    pending-OCR shots doesn't pin the watermark — those rows will be
    revisited on a later tick once OCR has populated the column.
    """
    cursor = await conn.execute(
        "SELECT id, ocr_text FROM screenshots "
        "WHERE id > ? AND ocr_text IS NOT NULL "
        "ORDER BY id ASC LIMIT ?",
        (int(after_id), int(limit)),
    )
    rows = await cursor.fetchall()
    return [(int(row["id"]), str(row["ocr_text"])) for row in rows]


#: kv key holding the auto-pin day budget as ``"YYYY-MM-DD:N"``.
DAILY_COUNTER_KV_KEY: str = "auto_pin_daily_count"


async def _count_pinned_today(conn: aiosqlite.Connection) -> int:
    """Return how many auto-pins this engine has recorded today (UTC).

    Schema note — the original implementation counted
    ``screenshots.pinned_at``, a column that has never existed: pinning is
    the ``tier = 'pinned'`` enum flipped in place, with no timestamp of its
    own (see :mod:`app.pinboard`). Every tick of this worker therefore died
    with ``no such column: pinned_at``.

    Because the flip carries no timestamp, "pins made today" cannot be
    recovered from ``screenshots`` at all — ``created_at`` is the *capture*
    time, so a backfill of old shots would never charge the cap and the
    runaway-regex guard this module exists for would be defeated. We keep a
    small day-stamped counter in ``kv_settings`` instead. Consequence, stated
    plainly: a manual pin from the UI no longer consumes auto-pin headroom.
    The cap still bounds what *this engine* can do in a day, which is the
    property that actually protects the database.
    """
    today_iso = datetime.now(tz=UTC).date().isoformat()
    raw = await get_kv(conn, DAILY_COUNTER_KV_KEY)
    if not raw or ":" not in raw:
        return 0
    day, _, count = raw.partition(":")
    if day != today_iso:
        return 0
    try:
        return max(0, int(count))
    except ValueError:
        return 0


async def _store_pinned_today(conn: aiosqlite.Connection, count: int) -> None:
    """Persist the day-stamped auto-pin counter read by :func:`_count_pinned_today`."""
    today_iso = datetime.now(tz=UTC).date().isoformat()
    await set_kv(conn, DAILY_COUNTER_KV_KEY, f"{today_iso}:{max(0, int(count))}")


def _compile_rule(rule: AutoPinRuleRow) -> re.Pattern[str] | None:
    """Compile one rule's regex; log and return ``None`` on failure.

    A bad pattern shouldn't crash the worker — the admin form already
    validates at insert time, so reaching this path means the row has
    been corrupted out-of-band. We log enough fields for the dashboard
    to surface the offender and skip it.
    """
    try:
        return re.compile(rule["pattern"], re.IGNORECASE)
    except re.error as exc:
        log.warning(
            "auto_pin_engine.compile_failed",
            rule_id=rule["id"],
            pattern=rule["pattern"],
            error=str(exc),
        )
        return None


async def _pin_screenshot(
    conn: aiosqlite.Connection,
    *,
    screenshot_id: int,
    now_iso: str,
) -> bool:
    """Flip a shot to ``tier = 'pinned'`` iff it isn't already pinned.

    Returns ``True`` iff exactly one row was flipped by this call. The
    ``WHERE tier != 'pinned'`` guard means re-running the engine against an
    already-pinned shot is a no-op and the daily-cap counter doesn't
    double-charge an existing pin.

    ``now_iso`` is accepted for call-site symmetry and logging only: the
    schema stores no pin timestamp.
    """
    del now_iso
    cursor = await conn.execute(
        "UPDATE screenshots SET tier = 'pinned' "
        "WHERE id = ? AND (tier IS NULL OR tier != 'pinned')",
        (int(screenshot_id),),
    )
    return int(cursor.rowcount) == 1


async def run_auto_pins(
    conn: aiosqlite.Connection,
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> AutoPinRunSummary:
    """Apply every enabled auto-pin rule above its per-rule watermark.

    For each rule:

    1. Read the watermark (defaults to 0 — full backfill on first run).
    2. Fetch up to :data:`DEFAULT_BATCH_LIMIT` shots above the watermark
       that have non-null ``ocr_text``.
    3. Run :func:`re.search` (``IGNORECASE``) against each shot's text.
    4. On match, if the running pin-count for today (UTC) is still
       below ``daily_cap``, write ``pinned_at`` and bump the counter.
       Otherwise mark ``daily_cap_hit`` and continue scanning so the
       watermark still advances and the rule drains.
    5. Advance the watermark to the maximum id we *inspected* — not
       the maximum id we *pinned* — so unmatched shots are not
       rescanned.

    Returns an :class:`AutoPinRunSummary` for the worker to log.
    """
    if daily_cap < 0:
        msg = "daily_cap must be non-negative"
        raise ValueError(msg)

    rules = await list_enabled_rules(conn)
    summary: AutoPinRunSummary = {
        "rules_processed": 0,
        "shots_pinned": 0,
        "daily_cap_hit": False,
    }
    if not rules:
        return summary

    pinned_today = await _count_pinned_today(conn)
    now_iso = datetime.now(tz=UTC).isoformat(timespec="seconds")

    for rule in rules:
        summary["rules_processed"] += 1
        watermark = await _read_watermark(conn, rule["id"])
        shots = await _fetch_candidate_shots(
            conn,
            after_id=watermark,
            limit=DEFAULT_BATCH_LIMIT,
        )
        if not shots:
            continue

        compiled = _compile_rule(rule)
        if compiled is None:
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
            if pinned_today >= daily_cap:
                summary["daily_cap_hit"] = True
                # Don't break — keep scanning so the watermark advances
                # over the remaining shots in this batch. Otherwise a
                # day full of matches would re-scan the same window
                # tomorrow without ever clearing the backlog.
                continue
            try:
                did_pin = await _pin_screenshot(
                    conn,
                    screenshot_id=sid,
                    now_iso=now_iso,
                )
            except aiosqlite.Error as exc:
                log.warning(
                    "auto_pin_engine.pin_failed",
                    rule_id=rule["id"],
                    screenshot_id=sid,
                    error=str(exc),
                )
                continue
            if did_pin:
                matched_in_rule += 1
                pinned_today += 1

        max_id = max(sid for sid, _ in shots)
        await _write_watermark(
            conn,
            rule_id=rule["id"],
            last_screenshot_id=max_id,
        )
        await conn.commit()

        summary["shots_pinned"] += matched_in_rule
        if matched_in_rule:
            await _store_pinned_today(conn, pinned_today)
            await conn.commit()
        if matched_in_rule:
            log.info(
                "auto_pin_engine.rule_matched",
                rule_id=rule["id"],
                pinned=matched_in_rule,
                watermark=max_id,
            )

    if summary["daily_cap_hit"]:
        log.warning(
            "auto_pin_engine.daily_cap_hit",
            daily_cap=daily_cap,
            pinned_today=pinned_today,
        )

    return summary


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "DEFAULT_DAILY_CAP",
    "AutoPinRuleRow",
    "AutoPinRunSummary",
    "list_enabled_rules",
    "run_auto_pins",
]
