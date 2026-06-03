"""Audit log replay — narrative grouping of a day's audit entries (v0.90 feature 1/3).

Reads :mod:`app.audit`'s append-only ``audit_log`` table for a single
calendar day and projects every row into one of five action *categories*
so an operator can scan "what changed in settings", "what got
bulk-deleted", "which tokens were touched", "which vault keys were
read/written" — all without paging through the raw timeline.

Design contract
---------------
* **Read-only.** Mirrors :mod:`app.audit_timeline` — this module never
  writes to ``audit_log``. The page is a textual *narrative* of the day,
  nothing more.
* **Parametrised SQL.** The day string is bound as a ``?`` parameter
  against SQLite's ``date(ts)`` function; the user-supplied path
  component is never spliced into the statement.
* **Stable category set.** Categories are
  ``settings``, ``bulk_delete``, ``tokens``, ``vault``, ``other`` — in
  that order. The replay page always renders the same five sections so
  the layout stays predictable even on a quiet day where some buckets
  are empty.
* **Never raise.** A transient SQLite hiccup yields a replay with zero
  entries in every bucket; the surrounding page render keeps working.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final, TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Sequence

log = get_logger("persona.audit.replay")

# Categories in canonical render order. The replay page always emits
# every bucket so a quiet day still presents a stable five-section
# skeleton instead of collapsing the layout. ``other`` is a deliberate
# catch-all so a brand-new action prefix never silently disappears.
_CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "settings",
    "bulk_delete",
    "tokens",
    "vault",
    "other",
)

# Prefix → category. The action slug convention is ``<feature>.<verb>``
# (see :mod:`app.audit`'s module docstring) so prefix matching on the
# leading dotted segment is the right level of granularity.
#
# Notes on bin choices:
# * ``bulk_delete`` covers only the literal ``bulk_delete.*`` prefix.
#   Sibling ``bulk_pin`` / ``bulk_collection_add`` actions fall through
#   to ``other`` on purpose — they are not destructive in the same way
#   and lumping them in would dilute the "what got bulk-deleted today"
#   story.
# * ``tokens`` matches both ``api_token.*`` and ``feed_token.*`` so a
#   reviewer sees every credential-shaped event in one place.
# * ``vault`` covers vault reads/writes/deletes and ``encrypted_notes.*``
#   because both modules write to the same secret-bearing storage tier.
_PREFIX_TO_CATEGORY: Final[dict[str, str]] = {
    "settings": "settings",
    "settings_api": "settings",
    "theme": "settings",
    "bulk_delete": "bulk_delete",
    "api_token": "tokens",
    "feed_token": "tokens",
    "vault": "vault",
    "encrypted_notes": "vault",
}

# Hard cap on rows pulled per day. Same ceiling as
# :mod:`app.audit_timeline` so the two views agree on "what happened
# today" and a noisy day cannot inflate the DOM past comfortable
# scrolling.
_MAX_ROWS_PER_DAY: Final[int] = 1_000

# Length of a bare ISO-8601 date (``YYYY-MM-DD``).
_ISO_DATE_LEN: Final[int] = 10


class ReplayEntry(TypedDict):
    """Single audit row as projected onto the replay page.

    Mirrors :class:`app.audit.AuditRow` field names so a caller already
    parsing the canonical audit endpoints can consume this payload with
    zero changes.
    """

    id: int
    ts: str
    action: str
    actor: str | None
    target: str | None
    detail: str | None
    success: bool


class ReplaySection(TypedDict):
    """One category bucket on the replay page."""

    category: str
    entries: list[ReplayEntry]


class ReplayPayload(TypedDict):
    """Top-level shape returned by :func:`build_replay`."""

    day: str
    total: int
    truncated: bool
    sections: list[ReplaySection]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_replay(day_iso: str) -> ReplayPayload:
    """Group a day's ``audit_log`` rows into ordered narrative sections.

    ``day_iso`` is a ``YYYY-MM-DD`` string. A malformed value falls back
    to *today* — same forgiving behaviour as :mod:`app.audit_timeline`,
    so a typo in the URL lands on a useful page instead of a 400.

    The returned :class:`ReplayPayload` always contains every entry in
    :data:`_CATEGORY_ORDER` — empty sections are kept so the template
    can render a stable five-section skeleton. Inside each section the
    entries are ordered ascending by ``ts`` then ``id`` so the section
    reads top-down chronologically; the *story* of settings changes is
    easier to follow forwards than backwards.
    """
    day_value = _parse_day_or_today(day_iso)
    rows = await _load_day_rows(day_value)

    buckets: dict[str, list[ReplayEntry]] = {cat: [] for cat in _CATEGORY_ORDER}
    for row in rows:
        category = _classify_action(row["action"])
        buckets[category].append(row)

    sections: list[ReplaySection] = [
        ReplaySection(category=cat, entries=buckets[cat]) for cat in _CATEGORY_ORDER
    ]

    payload: ReplayPayload = {
        "day": day_value.isoformat(),
        "total": len(rows),
        "truncated": len(rows) >= _MAX_ROWS_PER_DAY,
        "sections": sections,
    }
    log.info(
        "audit.replay.build",
        day=payload["day"],
        total=payload["total"],
        truncated=payload["truncated"],
        sections={cat: len(buckets[cat]) for cat in _CATEGORY_ORDER},
    )
    return payload


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _today_local() -> date:
    """Local-date "today" — matches the wall-clock day other day-views show."""
    return datetime.now().astimezone().date()


def _parse_day_or_today(day_iso: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to local today on any failure.

    Matches :mod:`app.audit_timeline._parse_day_or_today` — a malformed
    path component should not 400 an exploratory read-only view; it
    should surface a useful "today" replay instead.
    """
    if day_iso is None or day_iso == "":
        return _today_local()
    cleaned = day_iso.strip()
    if len(cleaned) != _ISO_DATE_LEN:
        log.info("audit.replay.day_invalid_fallback_today", value=day_iso)
        return _today_local()
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date()
    except ValueError:
        log.info("audit.replay.day_invalid_fallback_today", value=day_iso)
        return _today_local()


def _classify_action(action: str) -> str:
    """Map an action slug to one of the canonical categories.

    Uses the leading dotted segment as the prefix key. Unknown prefixes
    fall through to ``"other"`` so a freshly-added audit producer is
    *visible* on the replay page from day one without code changes to
    this module — it just won't get its own dedicated bucket until a
    follow-up patch updates :data:`_PREFIX_TO_CATEGORY`.
    """
    if not action:
        return "other"
    prefix = action.split(".", 1)[0]
    return _PREFIX_TO_CATEGORY.get(prefix, "other")


def _project_row(row: Any) -> ReplayEntry:
    """Build a single :class:`ReplayEntry` from an aiosqlite row.

    Mirrors :class:`app.audit.AuditRow`'s NULL-vs-empty semantics so
    downstream renderers (template + tests) can treat the two payloads
    identically.
    """
    return ReplayEntry(
        id=int(row["id"]),
        ts=str(row["ts"]),
        action=str(row["action"]),
        actor=(None if row["actor"] is None else str(row["actor"])),
        target=(None if row["target"] is None else str(row["target"])),
        detail=(None if row["detail"] is None else str(row["detail"])),
        success=bool(int(row["success"])),
    )


async def _load_day_rows(day_value: date) -> list[ReplayEntry]:
    """Fetch every audit row whose ``date(ts) = day_value`` (parametrised).

    The day value is bound through a ``?`` placeholder against SQLite's
    ``date(...)`` function — the user-supplied path component never
    touches the SQL string. Errors are swallowed and surfaced as an
    empty list so a transient SQLite hiccup renders an empty replay
    instead of 500-ing the page (same contract as
    :mod:`app.audit_timeline`).
    """
    day_str = day_value.strftime("%Y-%m-%d")
    sql = (
        "SELECT id, ts, action, actor, target, detail, success "
        "FROM audit_log "
        "WHERE date(ts) = ? "
        "ORDER BY ts ASC, id ASC "
        "LIMIT ?"
    )
    params: Sequence[object] = (day_str, _MAX_ROWS_PER_DAY)
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        log.warning(
            "audit.replay.load_failed",
            day=day_value.isoformat(),
            error=str(exc),
        )
        return []
    return [_project_row(row) for row in rows]


__all__ = [
    "ReplayEntry",
    "ReplayPayload",
    "ReplaySection",
    "build_replay",
]
