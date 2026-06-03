"""Bulk regex find-and-replace across ``screenshots.ocr_text`` (v0.77).

Persona v0.77 feature 3/3. The operator occasionally wants to repair a
systematic OCR error across the entire history — e.g. Tesseract has been
mis-reading a particular ligature, redacting a leaked passphrase that
shows up in dozens of shots, or normalising a company name that got
rendered three different ways. Doing that with sqlite3 by hand is
risky; doing it through Persona's recycle-bin per-row is tedious.

This module exposes the operation as a deliberate, audit-logged admin
primitive built on top of Python's :mod:`re` — *not* SQLite's ``REPLACE``
function — so the operator can use real regex (groups, anchors,
back-references) rather than a literal substring swap.

Two entry points
----------------
* :func:`preview` — pure dry-run. Compiles the regex, scans up to
  ``limit`` matching rows, returns ``before`` / ``after`` pairs so the UI
  can render a diff table. Never writes.
* :func:`apply`   — destructive. Compiles the regex first (so a broken
  pattern fails before any row is touched), then iterates the same set
  of rows and writes the substitution back. The ``UPDATE`` fires the
  ``screenshots_au`` trigger in :file:`schema.sql`, which keeps
  ``screenshots_fts`` consistent automatically — but we still issue an
  explicit ``INSERT INTO screenshots_fts(screenshots_fts) VALUES('rebuild')``
  after the bulk write as a belt-and-braces safety net in case a future
  refactor drops the trigger.

Safety + design notes
---------------------
* **Regex must compile before the apply path runs.** A
  :class:`re.error` is raised back to the caller as :class:`ValueError`
  so the route can render a friendly 400 rather than 500-ing.
* **Limit is hard-capped.** ``preview`` defaults to 100 rows and clamps
  to ``_PREVIEW_HARD_CAP``; ``apply`` defaults to 1000 and clamps to
  ``_APPLY_HARD_CAP``. A typo in the form cannot blow the loop up.
* **No-op writes are skipped.** If ``re.sub`` returns the original
  string unchanged we do not issue an UPDATE — that keeps the FTS
  trigger work proportional to actual changes.
* **NULL / empty rows are skipped.** Only rows with a non-NULL,
  non-empty ``ocr_text`` are considered candidates so a misfiring
  pattern cannot synthesise text on blank shots.
* **Parametrised SQL.** Every value travels via ``?`` placeholders.
  The regex is applied in Python, never interpolated into SQL.
* **Per-app scoping (v1.1).** Both :func:`preview` and :func:`apply`
  accept an optional ``app_name`` argument. When provided, the candidate
  query gains a ``WHERE app_name = ?`` predicate so the regex only ever
  runs against shots from that one app — the rest of the corpus is
  invisible. ``None`` (the default) preserves the v0.77 corpus-wide
  behaviour exactly. The value is bound as a parameter; the regex
  itself is still applied in Python, never glued into SQL. Per-app
  apply emits its own structured log under
  ``persona.ocr.find_replace.per_app`` so the audit reader can tell
  scoped runs apart from corpus-wide ones at a glance.
"""

from __future__ import annotations

import re
from typing import TypedDict

from app.logging_setup import get_logger
from app.ocr_history import record_snapshot
from app.storage.db import get_connection

log = get_logger("persona.ocr.find_replace")
log_per_app = get_logger("persona.ocr.find_replace.per_app")

# Hard caps on how many rows preview / apply will ever touch in a single
# call. The route layer also validates the user-supplied limit, but the
# caps here protect any future internal caller from a typo too.
_PREVIEW_HARD_CAP = 500
_APPLY_HARD_CAP = 10_000

# Default per-row truncation when shipping ``before`` / ``after`` strings
# to the preview table — full OCR text on a single shot can be tens of
# kilobytes which would balloon the HTMX response for no benefit.
_PREVIEW_SNIPPET_CHARS = 2_000


class PreviewRow(TypedDict):
    """One row of the dry-run preview returned by :func:`preview`."""

    shot_id: int
    before: str
    after: str


class ApplyResult(TypedDict):
    """Outcome summary returned by :func:`apply`."""

    scanned: int
    changed: int
    pattern: str
    replacement: str


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile ``pattern`` or raise :class:`ValueError` with the regex error.

    The route layer catches the :class:`ValueError` and renders a 400 so
    the user sees the real error message ("missing ), unterminated
    subpattern at position 7"), not a Python traceback in the logs.
    """
    cleaned = pattern or ""
    if not cleaned:
        msg = "pattern must not be empty"
        raise ValueError(msg)
    try:
        return re.compile(cleaned)
    except re.error as exc:
        msg = f"invalid regex: {exc}"
        raise ValueError(msg) from exc


def _clamp_limit(limit: int, cap: int) -> int:
    """Clamp ``limit`` into ``1..cap`` so a bad form value cannot loop forever."""
    return max(1, min(int(limit), cap))


def _truncate(text: str) -> str:
    """Cut the snippet sent to the preview UI down to a sane size."""
    if len(text) <= _PREVIEW_SNIPPET_CHARS:
        return text
    return text[:_PREVIEW_SNIPPET_CHARS] + "..."


def _normalise_app_name(app_name: str | None) -> str | None:
    """Trim whitespace and treat empty input as "no scope" (``None``).

    Keeps the route layer free of "is this an empty string or None?"
    bookkeeping — callers may pass either and the function treats them
    identically. Anything non-empty is returned stripped, ready to bind
    as a single ``?`` parameter against ``screenshots.app_name``.
    """
    if app_name is None:
        return None
    cleaned = app_name.strip()
    return cleaned if cleaned else None


async def preview(
    pattern: str,
    replacement: str,
    limit: int = 100,
    *,
    app_name: str | None = None,
) -> list[PreviewRow]:
    """Dry-run the regex against ``ocr_text`` and return up to ``limit`` diffs.

    Compiles ``pattern`` first (raises :class:`ValueError` on a bad
    regex), then streams rows where ``ocr_text`` is non-NULL and
    non-empty, applying :py:meth:`re.Pattern.sub` in Python and yielding
    only rows whose substitution actually changes the text. Rows where
    the pattern does not match are skipped silently so the preview only
    shows the impact, never the noise.

    When ``app_name`` is provided, the candidate query gains a
    ``WHERE app_name = ?`` predicate so only shots captured from that
    one app are scanned — useful for repairing a Tesseract mis-read
    that only ever happens in a specific font/UI. Passing ``None``
    (the default) preserves the v0.77 corpus-wide behaviour.

    Never writes — safe to call from any read-only context.
    """
    regex = _compile(pattern)
    safe_limit = _clamp_limit(limit, _PREVIEW_HARD_CAP)
    scoped_app = _normalise_app_name(app_name)

    rows: list[PreviewRow] = []
    async with get_connection() as conn:
        # SQLite cannot tell us "rows where re.sub would actually
        # change something" — that is a Python operation. So we scan
        # candidates (non-NULL, non-empty ocr_text) ordered newest
        # first, and stop as soon as we have collected ``safe_limit``
        # genuinely-changed rows. The hard-cap on ``scan_cap`` keeps
        # the worst case bounded when no rows match.
        #
        # Per-app scoping (v1.1) adds a parametrised ``app_name = ?``
        # predicate when the caller supplied a name. The SQL string is
        # built from two fixed branches (no f-string interpolation of
        # user data) and the value is always bound as a placeholder.
        scan_cap = safe_limit * 10
        if scoped_app is None:
            cursor = await conn.execute(
                "SELECT id, ocr_text FROM screenshots "
                "WHERE ocr_text IS NOT NULL AND ocr_text <> '' "
                "ORDER BY id DESC LIMIT ?",
                (scan_cap,),
            )
        else:
            cursor = await conn.execute(
                "SELECT id, ocr_text FROM screenshots "
                "WHERE ocr_text IS NOT NULL AND ocr_text <> '' "
                "AND app_name = ? "
                "ORDER BY id DESC LIMIT ?",
                (scoped_app, scan_cap),
            )
        async for row in cursor:
            before = str(row["ocr_text"])
            after = regex.sub(replacement, before)
            if after == before:
                continue
            rows.append(
                PreviewRow(
                    shot_id=int(row["id"]),
                    before=_truncate(before),
                    after=_truncate(after),
                )
            )
            if len(rows) >= safe_limit:
                break

    log.info(
        "ocr.find_replace.preview",
        pattern=pattern,
        replacement=replacement,
        limit=safe_limit,
        matched=len(rows),
        app_name=scoped_app,
    )
    if scoped_app is not None:
        log_per_app.info(
            "ocr.find_replace.preview.per_app",
            pattern=pattern,
            replacement=replacement,
            limit=safe_limit,
            matched=len(rows),
            app_name=scoped_app,
        )
    return rows


async def apply(
    pattern: str,
    replacement: str,
    limit: int = 1000,
    *,
    app_name: str | None = None,
) -> ApplyResult:
    """Execute the regex substitution against ``ocr_text``.

    Compiles ``pattern`` *before* opening any cursor so a syntactically
    invalid regex aborts the call without ever touching SQLite. Rows are
    scanned newest-first; for each candidate, :py:meth:`re.Pattern.sub`
    is applied in Python and the result is written back via an
    ``UPDATE`` only when it differs from the original (no-op writes are
    skipped so the FTS trigger work stays proportional to real changes).

    When ``app_name`` is provided, the candidate query is restricted to
    shots whose ``screenshots.app_name`` equals that value — every other
    row in the corpus is invisible to this call and will not be touched
    even if the regex would otherwise have matched. ``None`` (the
    default) preserves the v0.77 corpus-wide behaviour.

    After the loop, an explicit
    ``INSERT INTO screenshots_fts(screenshots_fts) VALUES('rebuild')``
    is issued. The ``screenshots_au`` trigger in :file:`schema.sql`
    already keeps FTS in sync per-UPDATE, so this rebuild is a
    belt-and-braces safety net rather than a correctness requirement —
    it costs at most a single full-text reindex and guarantees the FTS
    column reflects the post-replace ``ocr_text`` even if a future
    refactor removes or renames the trigger.

    Returns an :class:`ApplyResult` summarising scanned vs. changed
    counts so the caller can render a confirmation banner.
    """
    regex = _compile(pattern)
    safe_limit = _clamp_limit(limit, _APPLY_HARD_CAP)
    scoped_app = _normalise_app_name(app_name)

    scanned = 0
    changed = 0

    async with get_connection() as conn:
        # Same pattern as :func:`preview`: pull a bounded candidate set
        # in one query, then run the Python regex over each row, only
        # writing back the rows that actually change. Stop once we have
        # mutated ``safe_limit`` rows. The two SQL branches keep the
        # ``app_name = ?`` predicate parametrised; the user-supplied
        # value is never interpolated into the statement text.
        scan_cap = safe_limit * 10
        if scoped_app is None:
            cursor = await conn.execute(
                "SELECT id, ocr_text FROM screenshots "
                "WHERE ocr_text IS NOT NULL AND ocr_text <> '' "
                "ORDER BY id DESC LIMIT ?",
                (scan_cap,),
            )
        else:
            cursor = await conn.execute(
                "SELECT id, ocr_text FROM screenshots "
                "WHERE ocr_text IS NOT NULL AND ocr_text <> '' "
                "AND app_name = ? "
                "ORDER BY id DESC LIMIT ?",
                (scoped_app, scan_cap),
            )
        targets: list[tuple[int, str, str]] = []
        async for row in cursor:
            before = str(row["ocr_text"])
            after = regex.sub(replacement, before)
            scanned += 1
            if after == before:
                continue
            targets.append((int(row["id"]), before, after))
            if len(targets) >= safe_limit:
                break

        for shot_id, before, after in targets:
            # v0.92 — capture the pre-edit text so the operator can
            # revert a regex that ate too much. ``record_snapshot``
            # opens its own connection (which on aiosqlite is the same
            # underlying file-handle, serialised by the GIL + SQLite's
            # own locking) and silently no-ops on NULL/empty bodies.
            # We deliberately log per-shot inside the loop rather than
            # batch — the rows are bounded by ``_APPLY_HARD_CAP`` so the
            # extra round-trip is negligible vs. the regex work itself.
            await record_snapshot(shot_id, before, reason="find_replace")
            await conn.execute(
                "UPDATE screenshots SET ocr_text = ? WHERE id = ?",
                (after, shot_id),
            )
            changed += 1

        if changed > 0:
            # Belt-and-braces FTS refresh — see module docstring. This
            # statement is a SQLite FTS5 control command, not a row
            # insert; no values are interpolated.
            await conn.execute(
                "INSERT INTO screenshots_fts(screenshots_fts) VALUES('rebuild')"
            )

        await conn.commit()

    log.info(
        "ocr.find_replace.apply",
        pattern=pattern,
        replacement=replacement,
        limit=safe_limit,
        scanned=scanned,
        changed=changed,
        app_name=scoped_app,
    )
    if scoped_app is not None:
        # Dedicated per-app logger — lets the audit reader filter scoped
        # runs out of the much-noisier corpus-wide stream. The line above
        # already covers the global view; this is the focused signal.
        log_per_app.info(
            "ocr.find_replace.apply.per_app",
            pattern=pattern,
            replacement=replacement,
            limit=safe_limit,
            scanned=scanned,
            changed=changed,
            app_name=scoped_app,
        )
    return ApplyResult(
        scanned=scanned,
        changed=changed,
        pattern=pattern,
        replacement=replacement,
    )


__all__ = [
    "ApplyResult",
    "PreviewRow",
    "apply",
    "preview",
]
