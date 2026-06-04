"""Write-only Obsidian vault sync (markdown notebook).

Persona is the source of truth. The sync produces one markdown file per
day under ``<vault>/Persona/YYYY-MM-DD.md`` containing:

* H1 with the ISO date.
* TL;DR pulled from ``daily_digest`` (if any).
* Daily pin from ``daily_pin`` (one-line micro-summary).
* Hourly cards (one section per hour with apps + top words).
* Standalone notes from the ``notes`` table created that day.
* Pinned shots (``screenshots.tier = 'pinned'``) with a permalink-style
  reference and the captured app/window for context.

References that look like a ``path:line`` location are kept verbatim —
no escaping, no rewriting. Capitalised entities recorded in the
``entity`` table get wikilinked (``Denis`` → ``[[Denis]]``) only when
they are actually present in the rendered body.

The sync is write-only — Persona never reads anything back from the
vault. Files are skipped when the SHA-256 of the freshly-built markdown
matches the SHA-256 of the on-disk file (so an idle day is a true
no-op, not a touch-the-mtime no-op).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.obsidian_sync")

_DAY_FOLDER: str = "Persona"
"""Sub-folder inside the user's vault. Hard-coded so the layout stays
predictable across machines / re-syncs."""

_MAX_NOTE_BODY_CHARS: int = 2000
"""Truncate over-long notes (a single 50 KB stray clip would blow the
file size and is rarely useful in a daily summary anyway)."""

_MAX_PINNED_SHOTS_PER_DAY: int = 30
"""Don't dump 500 pinned thumbnails into one markdown file."""


# ---------------------------------------------------------------------------
# Public TypedDicts
# ---------------------------------------------------------------------------


class SyncResult(TypedDict):
    """Return shape of :func:`sync_to_vault`."""

    files_written: int
    files_skipped: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Day-window helpers
# ---------------------------------------------------------------------------


def _day_bounds_utc(day: date) -> tuple[str, str]:
    """Return ISO UTC ``[start, end_inclusive]`` strings for one day."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _parse_day(day_iso: str) -> date:
    """Parse ``YYYY-MM-DD`` strictly — caller bug if it does not match."""
    return date.fromisoformat(day_iso.strip())


# ---------------------------------------------------------------------------
# Row fetchers — each returns a small, fully-typed dict so the renderer
# never has to deal with raw sqlite rows.
# ---------------------------------------------------------------------------


async def _fetch_daily_digest(
    conn: aiosqlite.Connection, day_iso: str
) -> str | None:
    try:
        cursor = await conn.execute(
            "SELECT body FROM daily_digest WHERE day = ?",
            (day_iso,),
        )
        row = await cursor.fetchone()
    except sqlite3.OperationalError as exc:
        log.debug("obsidian_sync.fetch.daily_digest.skipped", error=str(exc))
        return None
    if row is None:
        return None
    body = row["body"]
    if body is None:
        return None
    text = str(body).strip()
    return text or None


async def _fetch_daily_pin(
    conn: aiosqlite.Connection, day_iso: str
) -> str | None:
    try:
        cursor = await conn.execute(
            "SELECT pin FROM daily_pin WHERE day = ?",
            (day_iso,),
        )
        row = await cursor.fetchone()
    except sqlite3.OperationalError as exc:
        log.debug("obsidian_sync.fetch.daily_pin.skipped", error=str(exc))
        return None
    if row is None:
        return None
    pin = row["pin"]
    if pin is None:
        return None
    text = str(pin).strip()
    return text or None


async def _fetch_hourly_cards(
    conn: aiosqlite.Connection, day: date
) -> list[dict[str, Any]]:
    start_iso, end_iso = _day_bounds_utc(day)
    try:
        cursor = await conn.execute(
            "SELECT hour_start, summary, apps_json, top_words, transcript_excerpt "
            "FROM hourly_card "
            "WHERE hour_start >= ? AND hour_start <= ? "
            "ORDER BY hour_start ASC",
            (start_iso, end_iso),
        )
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("obsidian_sync.fetch.hourly_cards.skipped", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "hour_start": str(row["hour_start"]),
                "summary": str(row["summary"] or "").strip(),
                "apps_json": str(row["apps_json"] or "").strip(),
                "top_words": str(row["top_words"] or "").strip(),
                "transcript_excerpt": str(
                    row["transcript_excerpt"] or ""
                ).strip(),
            }
        )
    return out


async def _fetch_standalone_notes(
    conn: aiosqlite.Connection, day: date
) -> list[dict[str, Any]]:
    start_iso, end_iso = _day_bounds_utc(day)
    try:
        cursor = await conn.execute(
            "SELECT id, title, body, source, created_at FROM notes "
            "WHERE created_at >= ? AND created_at <= ? "
            "ORDER BY created_at ASC",
            (start_iso, end_iso),
        )
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("obsidian_sync.fetch.notes.skipped", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        body_raw = str(row["body"] or "").strip()
        body = body_raw[:_MAX_NOTE_BODY_CHARS]
        truncated = len(body_raw) > _MAX_NOTE_BODY_CHARS
        out.append(
            {
                "id": int(row["id"]),
                "title": str(row["title"] or "").strip(),
                "body": body,
                "truncated": truncated,
                "source": str(row["source"] or "").strip(),
                "created_at": str(row["created_at"]),
            }
        )
    return out


async def _fetch_pinned_shots(
    conn: aiosqlite.Connection, day: date
) -> list[dict[str, Any]]:
    start_iso, end_iso = _day_bounds_utc(day)
    try:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, window_title, thumbnail_path "
            "FROM screenshots "
            "WHERE tier = 'pinned' "
            "  AND captured_at >= ? AND captured_at <= ? "
            "ORDER BY captured_at ASC "
            "LIMIT ?",
            (start_iso, end_iso, _MAX_PINNED_SHOTS_PER_DAY),
        )
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("obsidian_sync.fetch.pinned.skipped", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "captured_at": str(row["captured_at"]),
                "app_name": str(row["app_name"] or "").strip(),
                "window_title": str(row["window_title"] or "").strip(),
                "thumbnail_path": str(row["thumbnail_path"] or "").strip(),
            }
        )
    return out


async def _fetch_known_entities(
    conn: aiosqlite.Connection,
) -> list[str]:
    """Return every known entity name sorted longest-first.

    Sorting by length ensures multi-word entities ("Denis Pavlov") win
    over shorter prefixes ("Denis") when both happen to be in the
    ledger. Single-letter and empty rows are dropped — they'd produce
    junk wikilinks like ``[[A]]``. Missing ``entity`` table is treated
    as "no entities" so older Persona installs that pre-date migration
    110 still sync — they just produce markdown without wikilinks.
    """
    try:
        cursor = await conn.execute("SELECT name FROM entity")
        rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("obsidian_sync.fetch.entities.skipped", error=str(exc))
        return []
    names: list[str] = []
    for row in rows:
        raw = str(row["name"] or "").strip()
        if len(raw) >= 2:
            names.append(raw)
    names.sort(key=len, reverse=True)
    return names


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _format_hour_label(hour_start_iso: str) -> str:
    """Render ``2026-06-04T13:00:00+00:00`` as ``13:00 UTC`` for a header."""
    try:
        parsed = datetime.fromisoformat(hour_start_iso)
    except ValueError:
        return hour_start_iso
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%H:%M UTC")


def _format_clock(iso_ts: str) -> str:
    """Render ``YYYY-MM-DDTHH:MM:SS`` as ``HH:MM UTC``."""
    try:
        parsed = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%H:%M UTC")


def _parse_apps_json(blob: str) -> list[str]:
    """Best-effort top-app extraction from the hourly_card apps_json column.

    The producer writes either a JSON list of ``{"app": str, ...}`` or
    a plain comma-separated string; we accept both.
    """
    if not blob:
        return []
    text = blob.strip()
    if not text:
        return []
    if text.startswith("["):
        import json as _json  # noqa: PLC0415 — lazy

        try:
            data = _json.loads(text)
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        out: list[str] = []
        for item in data:
            if isinstance(item, dict):
                name = item.get("app") or item.get("name")
                if isinstance(name, str) and name.strip():
                    out.append(name.strip())
            elif isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out[:5]
    # Comma-separated fallback.
    return [piece.strip() for piece in text.split(",") if piece.strip()][:5]


def _wrap_one_entity(text: str, name: str) -> str:
    """Wrap every standalone occurrence of ``name`` in ``text`` with ``[[…]]``.

    Skips occurrences that are already inside an existing wikilink
    (detected via the two-char window before / after the match).
    Extracted from :func:`_wikilink_entities` so the regex callback
    closes over the *current* iteration's text, not a loop variable —
    keeps ruff B023 happy.
    """
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")
    pieces: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        start = match.start()
        end = match.end()
        already_open = start >= 2 and text[start - 2 : start] == "[["
        already_close = end + 2 <= len(text) and text[end : end + 2] == "]]"
        pieces.append(text[cursor:start])
        if already_open or already_close:
            pieces.append(match.group(0))
        else:
            pieces.append(f"[[{match.group(0)}]]")
        cursor = end
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


def _wikilink_entities(text: str, known_entities: list[str]) -> str:
    """Wrap every entity hit with ``[[…]]``.

    Whole-word match (``\\b``) so ``Denis`` does not match inside
    ``Denisov``. Sort order from :func:`_fetch_known_entities` makes
    the long-prefix-wins behaviour deterministic.
    """
    if not text or not known_entities:
        return text
    out = text
    for name in known_entities:
        out = _wrap_one_entity(out, name)
    return out


def _render_section_header(level: int, text: str) -> str:
    return ("#" * level) + " " + text


def _render_header(day_iso: str) -> list[str]:
    return [
        f"# {day_iso}",
        "",
        (
            "Captured by Persona — daily summary, hourly cards, notes, and "
            "pinned shots. Persona is the source of truth; this file is "
            "regenerated on each sync."
        ),
        "",
    ]


def _render_text_section(
    heading: str, body: str | None, entities: list[str], empty_msg: str
) -> list[str]:
    out: list[str] = [_render_section_header(2, heading)]
    if body:
        out.append(_wikilink_entities(body, entities))
    else:
        out.append(empty_msg)
    out.append("")
    return out


def _render_hourly_section(
    cards: list[dict[str, Any]], entities: list[str]
) -> list[str]:
    out: list[str] = [_render_section_header(2, "Hourly cards")]
    if not cards:
        out.append("_No hourly cards for this day._")
        out.append("")
        return out
    for card in cards:
        out.append(_render_section_header(3, _format_hour_label(card["hour_start"])))
        apps = _parse_apps_json(card["apps_json"])
        if apps:
            out.append("- Apps: " + ", ".join(apps))
        if card["top_words"]:
            out.append("- Top words: " + card["top_words"])
        if card["summary"]:
            out.append("")
            out.append(_wikilink_entities(card["summary"], entities))
        out.append("")
    return out


def _render_notes_section(
    notes: list[dict[str, Any]], entities: list[str]
) -> list[str]:
    out: list[str] = [_render_section_header(2, "Notes")]
    if not notes:
        out.append("_No standalone notes for this day._")
        out.append("")
        return out
    for note in notes:
        title = note["title"] or f"Note #{note['id']}"
        out.append(_render_section_header(3, title))
        meta_bits: list[str] = [_format_clock(note["created_at"])]
        if note["source"]:
            meta_bits.append(f"source: {note['source']}")
        out.append("_" + " · ".join(meta_bits) + "_")
        out.append("")
        out.append(_wikilink_entities(note["body"], entities))
        if note["truncated"]:
            out.append("")
            out.append(
                f"_(truncated to {_MAX_NOTE_BODY_CHARS} chars — open in "
                "Persona for the full note)_"
            )
        out.append("")
    return out


def _render_pinned_section(pinned: list[dict[str, Any]]) -> list[str]:
    out: list[str] = [_render_section_header(2, "Pinned shots")]
    if not pinned:
        out.append("_No pinned shots for this day._")
        out.append("")
        return out
    for shot in pinned:
        clock = _format_clock(shot["captured_at"])
        context_parts: list[str] = []
        if shot["app_name"]:
            context_parts.append(shot["app_name"])
        if shot["window_title"]:
            context_parts.append(shot["window_title"])
        context = " — ".join(context_parts) if context_parts else "shot"
        link = f"[shot #{shot['id']}](/shot/{shot['id']})"
        out.append(f"- {clock} · {link} · {context}")
    out.append("")
    return out


async def _render_markdown(
    conn: aiosqlite.Connection,
    day: date,
) -> str:
    day_iso = day.isoformat()

    digest = await _fetch_daily_digest(conn, day_iso)
    pin = await _fetch_daily_pin(conn, day_iso)
    hourly = await _fetch_hourly_cards(conn, day)
    notes = await _fetch_standalone_notes(conn, day)
    pinned = await _fetch_pinned_shots(conn, day)
    entities = await _fetch_known_entities(conn)

    lines: list[str] = []
    lines.extend(_render_header(day_iso))
    lines.extend(
        _render_text_section("TL;DR", digest, entities, "_No digest for this day._")
    )
    lines.extend(
        _render_text_section("Daily pin", pin, entities, "_No pin for this day._")
    )
    lines.extend(_render_hourly_section(hourly, entities))
    lines.extend(_render_notes_section(notes, entities))
    lines.extend(_render_pinned_section(pinned))

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_day_markdown(day_iso: str) -> str:
    """Return the markdown body for one day. Pure read.

    ``day_iso`` must be ``YYYY-MM-DD``. The connection is opened once
    and re-used for every fetch.
    """
    day = _parse_day(day_iso)
    async with get_connection() as conn:
        body = await _render_markdown(conn, day)
    return body


# ---------------------------------------------------------------------------
# Vault safety + file writing
# ---------------------------------------------------------------------------


class VaultSafetyError(ValueError):
    """Raised when ``vault_path`` is not a safe target for sync."""


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_vault_path(vault_path: Path) -> Path:
    """Check that ``vault_path`` is absolute, exists, and is not Persona itself.

    The user is about to be asked to type a directory into a settings
    form. If that directory happens to overlap with Persona's data
    folder or the Persona checkout, writing markdown into it could
    overwrite source files or the SQLite database. We refuse in those
    cases with a helpful hint.

    Returns the resolved absolute path on success.
    """
    if not isinstance(vault_path, Path):
        raise VaultSafetyError("vault_path must be a Path")
    if not vault_path.is_absolute():
        raise VaultSafetyError(
            "vault_path must be absolute (start with / or a drive letter)"
        )

    resolved = vault_path.expanduser().resolve()

    if not resolved.exists():
        raise VaultSafetyError(
            f"vault_path does not exist: {resolved}"
        )
    if not resolved.is_dir():
        raise VaultSafetyError(
            f"vault_path is not a directory: {resolved}"
        )

    settings = get_settings()
    data_dir = settings.data_dir.expanduser().resolve()
    db_dir = settings.db_path.expanduser().resolve().parent
    # Repo root := parent of the ``app`` package directory.
    repo_root = Path(__file__).resolve().parent.parent

    for forbidden, label in (
        (data_dir, "Persona data directory"),
        (db_dir, "Persona SQLite directory"),
        (repo_root, "Persona source checkout"),
    ):
        if resolved == forbidden or _is_subpath(resolved, forbidden):
            raise VaultSafetyError(
                f"vault_path overlaps the {label} ({forbidden}). "
                "Pick a different folder so the sync cannot overwrite "
                "your own database or source files."
            )

    # Probe write access by touching a tempfile inside the resolved path.
    probe = resolved / ".persona-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise VaultSafetyError(
            f"vault_path is not writable: {resolved} ({exc})"
        ) from exc

    return resolved


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _existing_hash(target: Path) -> str | None:
    if not target.exists():
        return None
    try:
        existing = target.read_text(encoding="utf-8")
    except OSError:
        return None
    return _hash_text(existing)


async def sync_to_vault(
    vault_path: Path,
    days: int = 7,
) -> SyncResult:
    """Sync the last ``days`` days into ``<vault>/Persona/``.

    The window includes today and the (days-1) previous days, oldest
    first. For each day we build markdown and, if the SHA-256 differs
    from what's already on disk, write the file atomically (temp-write
    + rename).

    Skips writes when content has not changed so syncs are cheap on
    quiet days. Per-file failures land in ``errors`` so one broken day
    does not poison the rest of the window.
    """
    if days < 1:
        return SyncResult(files_written=0, files_skipped=0, errors=[])

    try:
        resolved_vault = validate_vault_path(vault_path)
    except VaultSafetyError as exc:
        log.warning(
            "obsidian_sync.vault.invalid",
            vault_path=str(vault_path),
            error=str(exc),
        )
        return SyncResult(
            files_written=0,
            files_skipped=0,
            errors=[f"vault: {exc}"],
        )

    target_folder = resolved_vault / _DAY_FOLDER
    try:
        target_folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "obsidian_sync.mkdir.failed",
            target=str(target_folder),
            error=str(exc),
        )
        return SyncResult(
            files_written=0,
            files_skipped=0,
            errors=[f"mkdir: {exc}"],
        )

    today_utc = datetime.now(tz=UTC).date()
    window: list[date] = [
        today_utc - timedelta(days=offset)
        for offset in range(days - 1, -1, -1)
    ]

    written = 0
    skipped = 0
    errors: list[str] = []

    for day in window:
        day_iso = day.isoformat()
        target = target_folder / f"{day_iso}.md"
        try:
            body = await build_day_markdown(day_iso)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "obsidian_sync.build.failed",
                day=day_iso,
                error=str(exc),
            )
            errors.append(f"build {day_iso}: {exc}")
            continue

        new_hash = _hash_text(body)
        old_hash = _existing_hash(target)
        if old_hash is not None and old_hash == new_hash:
            skipped += 1
            log.debug("obsidian_sync.skip", day=day_iso, path=str(target))
            continue

        tmp = target.with_suffix(".md.tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            log.warning(
                "obsidian_sync.write.failed",
                day=day_iso,
                target=str(target),
                error=str(exc),
            )
            errors.append(f"write {day_iso}: {exc}")
            continue
        written += 1
        log.info(
            "obsidian_sync.write",
            day=day_iso,
            target=str(target),
            bytes=len(body.encode("utf-8")),
        )

    log.info(
        "obsidian_sync.cycle",
        files_written=written,
        files_skipped=skipped,
        errors=len(errors),
        days=days,
        vault=str(resolved_vault),
    )
    return SyncResult(
        files_written=written,
        files_skipped=skipped,
        errors=errors,
    )


__all__ = [
    "SyncResult",
    "VaultSafetyError",
    "build_day_markdown",
    "sync_to_vault",
    "validate_vault_path",
]
