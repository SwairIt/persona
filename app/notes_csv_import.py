"""Bulk import of standalone notes from a CSV blob (v1.8 feature 3/3).

The watch-folder importer in :mod:`app.workers` (see ``039_inbox_notes.sql``
and :mod:`app.storage.notes`) handles single ``.md`` drops one file at a
time. That is the right shape for the day-to-day "drop a note in the
inbox folder" workflow but a poor fit for the *bulk* case: migrating a
shoebox of plain-text notes off Evernote / Bear / Obsidian, hydrating a
fresh install from a previous Persona export, or seeding a demo
instance for a screencast. This module is the bulk path.

Public contract
---------------
:func:`import_notes_csv` accepts an in-memory CSV string (the route
already enforces the 5 MiB cap before handing it to us — keeping the
disk-vs-memory decision *out* of this module lets the same entry point
power both the HTTP upload form and the ``persona import-notes-csv``
CLI subcommand without divergence).

Expected columns
~~~~~~~~~~~~~~~~
* ``body``        — required. Empty / whitespace-only bodies are skipped
  with an ``empty_body`` error so the operator can spot truncated rows.
* ``title``       — optional. Empty values become ``NULL`` rather than
  the empty string, matching the convention used by every other writer
  on the ``notes`` table (see :func:`app.storage.notes.insert_inbox_note`).
* ``created_at``  — optional ISO-8601 timestamp. Bad values are recorded
  in ``errors`` and the row is skipped — silently coercing a malformed
  date to ``datetime('now')`` would mask data-loss bugs in the source
  export.
* ``tags``        — optional. Comma-separated tag names; merged with
  ``default_tags`` and lower-cased + stripped before being attached via
  :func:`app.storage.notes.add_tag`. Empty fragments are dropped.

Idempotency
-----------
The same CSV imported twice writes each note **exactly once**. We pull
the SHA-256 of every existing plaintext ``notes.body`` into a Python
``set`` *once* at the start of the run and compare each incoming row
against it. The hash is computed on the normalised body (UTF-8 bytes,
trailing whitespace stripped, line-endings unified to ``\\n``) so a
trivial CRLF-vs-LF difference between exports does not produce
duplicates.

Encrypted notes (``encrypted = 1``) are deliberately excluded from the
hash set — their ``body`` column is the empty string and including it
would make the very first plaintext row collide with every locked note.
That is correct: the only way to deduplicate against an encrypted row
is to decrypt it, which would require the master password, which we
deliberately do not have at import time. A re-imported plaintext row
that happens to match an encrypted row's hidden plaintext will produce
a duplicate; the alternative — failing the import entirely — is worse.

Return shape
------------
``{"imported": int, "skipped": int, "errors": list[dict[str, ...]]}``.
``errors`` is a list of dicts ``{"row": int, "reason": str}`` so the
caller can render a per-row diagnostic in the UI without needing to
re-parse the CSV. ``row`` is the 1-based index of the data row (header
excluded) — matches how spreadsheet apps number rows for the operator.

Why one big transaction
-----------------------
The whole import runs inside a single ``aiosqlite`` connection so a
``BEGIN`` … ``COMMIT`` envelope is naturally cheap; we commit per row
to keep partial progress durable even if the operator cancels the
upload mid-stream. The alternative (one giant transaction) would mean
``import_notes_csv`` aborts the entire batch on the first bad row,
which is hostile to the typical "spreadsheet has one weird row"
scenario.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime
from typing import TYPE_CHECKING, Final, TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.notes import add_tag, insert_inbox_note

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

log = get_logger("persona.notes_csv_import")

# Marker stamped into ``notes.source`` so the inbox listing can tell
# CSV-imported rows apart from watch-folder drops (``.md``) and the
# encrypted-note backfill (``encrypted``). Single literal so a future
# change is a one-line edit.
_SOURCE_TAG: Final = "csv_import"

# How big a single row's body is allowed to be before we refuse it.
# The HTTP route caps the whole upload at 5 MiB; this per-row cap stops
# one runaway cell from filling the entire budget.
_MAX_BODY_BYTES: Final = 1_000_000

# Required header. We refuse to parse anything that does not declare
# ``body`` because every other column is optional — without ``body``
# there is nothing to insert.
_REQUIRED_COLUMN: Final = "body"

# Recognised optional headers. Anything else is silently ignored so a
# spreadsheet with extra notation columns does not 400 the upload.
_OPTIONAL_COLUMNS: Final[frozenset[str]] = frozenset(
    {"title", "created_at", "tags"}
)


class _ImportError(TypedDict):
    """One per-row failure surfaced back to the operator UI."""

    row: int
    reason: str


class ImportResult(TypedDict):
    """Public return shape — exposed by the route + CLI both."""

    imported: int
    skipped: int
    errors: list[_ImportError]


def _normalise_body(raw: str) -> str:
    """Canonicalise a body for hashing + storage.

    Strips trailing whitespace and unifies line endings to ``\\n`` so a
    Windows-exported note and a Unix-exported note with otherwise
    identical content hash to the same value. We deliberately keep
    *leading* whitespace — Markdown indentation is semantic (code
    blocks, nested lists) and trimming it would corrupt the note.
    """
    unified = raw.replace("\r\n", "\n").replace("\r", "\n")
    return unified.rstrip()


def _body_hash(normalised: str) -> str:
    """SHA-256 hex digest of a UTF-8 encoded normalised body."""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _parse_tags(raw: str | None, defaults: Sequence[str]) -> list[str]:
    """Merge per-row ``tags`` column with the form-level ``default_tags``.

    Both sides are normalised the same way: split on comma, strip,
    lower-case, drop empties, deduplicate while preserving first-seen
    order so the operator's intent is reflected in the eventual
    ``note_tags`` join (the column has no semantic ordering itself, but
    the audit-friendly log line below shows the merged set).
    """
    pieces: list[str] = []
    if raw:
        pieces.extend(raw.split(","))
    pieces.extend(defaults)
    seen: set[str] = set()
    ordered: list[str] = []
    for piece in pieces:
        cleaned = piece.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


def _parse_created_at(raw: str | None) -> tuple[str | None, str | None]:
    """Validate an optional ISO-8601 ``created_at`` column.

    Returns ``(value, None)`` on success or ``(None, reason)`` on
    failure. ``value`` is the original string (we do not reformat it —
    SQLite stores it verbatim) and ``reason`` is the operator-facing
    error string when parsing fails. An empty / missing column is *not*
    an error: it returns ``(None, None)`` so the caller can fall back to
    SQLite's ``DEFAULT (datetime('now'))``.
    """
    if raw is None:
        return None, None
    cleaned = raw.strip()
    if not cleaned:
        return None, None
    try:
        # ``fromisoformat`` is liberal enough for the common shapes
        # (``2024-01-02T15:04:05``, ``2024-01-02 15:04:05``, bare dates)
        # while still rejecting obvious nonsense.
        datetime.fromisoformat(cleaned)
    except ValueError:
        return None, f"invalid created_at: {cleaned!r}"
    return cleaned, None


async def _existing_body_hashes(conn: aiosqlite.Connection) -> set[str]:
    """SHA-256 set of every plaintext ``notes.body`` currently in the DB.

    Encrypted rows are excluded — their ``body`` is the empty string
    and including them would dedupe every empty-body row away. The
    parametrised ``WHERE encrypted = ?`` reuses the partial index from
    ``045_encrypted_notes.sql`` (which targets the inverse predicate)
    so the scan is fast even on a multi-thousand-row notes table.
    """
    hashes: set[str] = set()
    cursor = await conn.execute(
        "SELECT body FROM notes WHERE encrypted = ?",
        (0,),
    )
    async for row in cursor:
        body = str(row["body"] or "")
        hashes.add(_body_hash(_normalise_body(body)))
    await cursor.close()
    return hashes


def _validate_header(fieldnames: Iterable[str] | None) -> str | None:
    """Return ``None`` on a valid header, or a human-readable error message."""
    if not fieldnames:
        return "missing CSV header"
    columns = {name.strip().lower() for name in fieldnames if name}
    if _REQUIRED_COLUMN not in columns:
        return f"missing required column: {_REQUIRED_COLUMN!r}"
    return None


async def _insert_row(
    conn: aiosqlite.Connection,
    *,
    body: str,
    title: str | None,
    created_at: str | None,
    tag_names: Sequence[str],
) -> int:
    """Insert a single note row and attach its tags. Returns the new ``notes.id``.

    Two SQL paths because SQLite ignores ``DEFAULT`` when the column is
    listed in the ``INSERT`` — so we either skip ``created_at``
    (default-now path) or include it explicitly. Parametrised either
    way; no string interpolation ever touches the operator input.
    """
    if created_at is None:
        note_id = await insert_inbox_note(
            conn,
            body=body,
            title=title,
            source=_SOURCE_TAG,
        )
    else:
        cursor = await conn.execute(
            "INSERT INTO notes (title, body, source, created_at) "
            "VALUES (?, ?, ?, ?)",
            (title, body, _SOURCE_TAG, created_at),
        )
        await conn.commit()
        row_id = cursor.lastrowid
        if row_id is None:
            msg = "INSERT INTO notes did not return a row id"
            raise RuntimeError(msg)
        note_id = int(row_id)

    for name in tag_names:
        try:
            await add_tag(conn, note_id, name)
        except ValueError:
            # ``add_tag`` rejects empty names — should not happen here
            # because ``_parse_tags`` drops them, but a stray
            # whitespace-only tag would otherwise abort the whole row.
            continue
    return note_id


def _row_body(row: dict[str, str | None]) -> str:
    """Lift the ``body`` column out of a DictReader row, handling None."""
    raw = row.get(_REQUIRED_COLUMN)
    if raw is None:
        return ""
    return str(raw)


def _row_title(row: dict[str, str | None]) -> str | None:
    """Extract the optional ``title`` column; ``""`` collapses to ``None``."""
    raw = row.get("title")
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned or None


async def import_notes_csv(
    text: str,
    default_tags: Sequence[str] | None = None,
) -> ImportResult:
    """Parse ``text`` as CSV and bulk-insert each row into ``notes``.

    ``default_tags`` are attached to every successfully imported note in
    addition to whatever the row's own ``tags`` column carried — handy
    for tagging a whole import (``--tags evernote,migration``) without
    rewriting the source file.

    The return shape mirrors the spec exactly: ``imported`` counts the
    rows that produced a fresh ``notes`` insert, ``skipped`` counts the
    rows that hashed to an existing body (idempotent re-runs), and
    ``errors`` carries per-row diagnostics that the UI surfaces verbatim.

    Never raises on operator input — a malformed CSV header returns
    early with a single ``errors`` entry on row ``0`` so the route can
    still respond with a 200 + a rendered error list rather than
    bouncing the whole upload with a 500.
    """
    defaults = list(default_tags or [])
    result: ImportResult = {"imported": 0, "skipped": 0, "errors": []}

    reader = csv.DictReader(io.StringIO(text))
    header_error = _validate_header(reader.fieldnames)
    if header_error is not None:
        result["errors"].append({"row": 0, "reason": header_error})
        log.warning("notes_csv_import.bad_header", reason=header_error)
        return result

    async with get_connection() as conn:
        seen_hashes = await _existing_body_hashes(conn)
        # In-batch dedup: if the same body appears twice in the CSV we
        # only insert it once. Tracking it locally avoids a re-scan of
        # the DB after each insert.
        for row_index, raw_row in enumerate(reader, start=1):
            body_raw = _row_body(raw_row)
            normalised = _normalise_body(body_raw)
            if not normalised:
                result["errors"].append(
                    {"row": row_index, "reason": "empty_body"}
                )
                continue
            if len(normalised.encode("utf-8")) > _MAX_BODY_BYTES:
                result["errors"].append(
                    {"row": row_index, "reason": "body too large"}
                )
                continue

            digest = _body_hash(normalised)
            if digest in seen_hashes:
                result["skipped"] += 1
                continue

            created_at, created_err = _parse_created_at(raw_row.get("created_at"))
            if created_err is not None:
                result["errors"].append(
                    {"row": row_index, "reason": created_err}
                )
                continue

            tag_names = _parse_tags(raw_row.get("tags"), defaults)
            title = _row_title(raw_row)

            try:
                await _insert_row(
                    conn,
                    body=normalised,
                    title=title,
                    created_at=created_at,
                    tag_names=tag_names,
                )
            except (aiosqlite.Error, RuntimeError) as exc:
                # One bad row must not abort the rest of the import.
                result["errors"].append(
                    {"row": row_index, "reason": f"db error: {exc}"}
                )
                continue

            seen_hashes.add(digest)
            result["imported"] += 1

    log.info(
        "notes_csv_import.done",
        imported=result["imported"],
        skipped=result["skipped"],
        errors=len(result["errors"]),
        default_tags=defaults,
    )
    return result


__all__ = ["ImportResult", "import_notes_csv"]
