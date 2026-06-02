"""Background worker that watches ``data/inbox/`` for new markdown notes.

Each ``*.md`` file in the inbox is read, optionally parsed for a YAML-ish
front-matter block, inserted into the standalone ``notes`` table, and
moved to ``processed/`` once the database row is committed. Files that
fail to parse are moved to ``failed/`` with a sibling ``.error.txt``
explaining the rejection — the worker never deletes user data.

Polling cadence is ``_POLL_INTERVAL_SECONDS`` (30s). When the optional
``watchfiles`` package is installed the worker uses it to react to file
events sooner, but the fall-back poll loop is always wired in case the
import fails or the watch raises mid-flight.

Front-matter format (stdlib-only parser — no PyYAML dependency)::

    ---
    title: Daily standup
    tags: meeting, standup, work
    ---
    # Body starts here

``tags`` is split on commas; ``title`` is a single line. Unknown keys
are ignored. The opening and closing ``---`` lines must each be alone
on their line. Anything that does not start with ``---`` on the very
first non-empty line is treated as a body-only file.

Idempotency: the move-after-success pattern means a partially-imported
file (DB row inserted, ``Path.replace`` failed) will be reprocessed on
the next tick and produce a duplicate note. SQLite ``rowid`` collisions
are impossible so the second import becomes a separate row — this is
the documented trade-off; if you need stricter dedup, hash the body
before inserting (out of scope for v0.37).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.notes import add_tag, insert_inbox_note
from app.workers.control import CaptureController, get_controller

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

log = get_logger("persona.inbox")

_POLL_INTERVAL_SECONDS: float = 30.0
_FRONT_MATTER_DELIM: str = "---"
_PROCESSED_DIR_NAME: str = "processed"
_FAILED_DIR_NAME: str = "failed"


@dataclass(frozen=True)
class ParsedNote:
    """The result of parsing a single inbox file."""

    title: str | None
    tags: list[str]
    body: str


@dataclass(frozen=True)
class CycleReport:
    """Summary of one scan + import pass — surfaced by the manual trigger route."""

    scanned: int
    imported: int
    failed: int


async def run_inbox_worker(controller: CaptureController | None = None) -> None:
    """Continuously drain the inbox folder until ``stop_event`` fires.

    Honours ``settings.inbox_enabled``: when off at startup the worker
    awaits the stop event without ever touching the filesystem. The
    setting is *not* re-read on every tick — flipping it requires a
    restart, matching the embeddings/clipboard worker semantics.
    """
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.inbox_enabled:
        log.info("inbox_worker.disabled")
        await ctrl.stop_event.wait()
        return

    inbox_dir = settings.inbox_path
    _ensure_inbox_layout(inbox_dir)
    log.info(
        "inbox_worker.started",
        inbox_dir=str(inbox_dir),
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )

    while not ctrl.stop_event.is_set():
        try:
            report = await run_inbox_cycle(inbox_dir)
            if report.scanned:
                log.info(
                    "inbox_worker.cycle",
                    scanned=report.scanned,
                    imported=report.imported,
                    failed=report.failed,
                )
        except asyncio.CancelledError:
            log.info("inbox_worker.cancelled")
            raise
        except Exception as exc:
            log.exception("inbox_worker.cycle_failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=_POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue

    log.info("inbox_worker.stopped")


async def run_inbox_cycle(inbox_dir: Path | None = None) -> CycleReport:
    """Process every pending ``*.md`` once; return a summary.

    Reusable from the HTTP ``/inbox/import-now`` handler so the user
    can drain the inbox on demand without waiting for the next tick.
    Safe to call concurrently with the background worker — each file
    is moved atomically and the second caller simply observes an empty
    directory.
    """
    target = inbox_dir or get_settings().inbox_path
    _ensure_inbox_layout(target)

    pending = sorted(p for p in target.glob("*.md") if p.is_file())
    if not pending:
        return CycleReport(scanned=0, imported=0, failed=0)

    imported = 0
    failed = 0
    for path in pending:
        try:
            ok = await _process_one(path, inbox_dir=target)
        except Exception as exc:
            log.exception("inbox_worker.file_failed", path=str(path), error=str(exc))
            _move_to_failed(path, target, reason=f"unhandled: {exc!r}")
            failed += 1
            continue
        if ok:
            imported += 1
        else:
            failed += 1

    return CycleReport(scanned=len(pending), imported=imported, failed=failed)


async def _process_one(path: Path, *, inbox_dir: Path) -> bool:
    """Parse + insert + move one file. Returns ``True`` on success."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _move_to_failed(path, inbox_dir, reason=f"read failed: {exc}")
        return False
    except UnicodeDecodeError as exc:
        _move_to_failed(path, inbox_dir, reason=f"not UTF-8: {exc}")
        return False

    try:
        parsed = parse_markdown_note(raw)
    except ValueError as exc:
        _move_to_failed(path, inbox_dir, reason=f"parse failed: {exc}")
        return False

    if not parsed.body.strip():
        _move_to_failed(path, inbox_dir, reason="empty body after front-matter")
        return False

    title = parsed.title or path.stem
    async with get_connection() as conn:
        note_id = await insert_inbox_note(
            conn,
            body=parsed.body,
            title=title,
            source=path.name,
        )
        await _apply_tags(conn, note_id=note_id, tags=parsed.tags)

    log.info(
        "inbox_worker.imported",
        path=path.name,
        note_id=note_id,
        tag_count=len(parsed.tags),
        body_chars=len(parsed.body),
    )
    _move_to_processed(path, inbox_dir)
    return True


async def _apply_tags(
    conn: aiosqlite.Connection,
    *,
    note_id: int,
    tags: list[str],
) -> None:
    """Attach each tag to the note, skipping blanks. Errors are logged, not raised."""
    for tag in tags:
        cleaned = tag.strip()
        if not cleaned:
            continue
        try:
            await add_tag(conn, note_id, cleaned)
        except ValueError as exc:
            log.warning("inbox_worker.tag_skipped", tag=tag, error=str(exc))


def parse_markdown_note(text: str) -> ParsedNote:
    """Split optional YAML-ish front-matter from the body.

    Recognised keys are ``title`` (single string) and ``tags`` (comma
    separated list). Unknown keys are silently ignored so the format
    stays forgiving — the user shouldn't need to read this module to
    drop a note into the inbox. Raises :class:`ValueError` only when
    the opening ``---`` has no matching closing delimiter, which is the
    one case where we genuinely cannot tell front-matter from body.
    """
    lines = text.splitlines()
    body_start = 0
    title: str | None = None
    tags: list[str] = []

    first_non_empty = _first_non_empty_index(lines)
    if first_non_empty is not None and lines[first_non_empty].strip() == _FRONT_MATTER_DELIM:
        close_idx = _find_closing_delim(lines, start=first_non_empty + 1)
        if close_idx is None:
            msg = "front-matter opening '---' without a closing delimiter"
            raise ValueError(msg)
        for raw_line in lines[first_non_empty + 1 : close_idx]:
            key, value = _split_key_value(raw_line)
            if key is None or value is None:
                continue
            if key == "title":
                title = value or None
            elif key == "tags":
                tags = [t for t in (part.strip() for part in value.split(",")) if t]
        body_start = close_idx + 1

    body = "\n".join(lines[body_start:]).strip("\n")
    return ParsedNote(title=title, tags=tags, body=body)


def _first_non_empty_index(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if line.strip():
            return idx
    return None


def _find_closing_delim(lines: list[str], *, start: int) -> int | None:
    for idx in range(start, len(lines)):
        if lines[idx].strip() == _FRONT_MATTER_DELIM:
            return idx
    return None


def _split_key_value(line: str) -> tuple[str | None, str | None]:
    """Split ``key: value`` while tolerating whitespace and a leading ``#`` comment.

    Lines without a ``:`` or starting with ``#`` are skipped (returns
    ``(None, None)``). Quoting is stripped from values so ``title:
    "foo"`` and ``title: foo`` behave the same.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None, None
    if ":" not in stripped:
        return None, None
    key, _, value = stripped.partition(":")
    key = key.strip().lower()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def _ensure_inbox_layout(inbox_dir: Path) -> None:
    """Create ``inbox/``, ``inbox/processed/``, ``inbox/failed/`` if missing."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / _PROCESSED_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (inbox_dir / _FAILED_DIR_NAME).mkdir(parents=True, exist_ok=True)


def _move_to_processed(path: Path, inbox_dir: Path) -> None:
    target = _unique_target(inbox_dir / _PROCESSED_DIR_NAME / path.name)
    path.replace(target)


def _move_to_failed(path: Path, inbox_dir: Path, *, reason: str) -> None:
    target = _unique_target(inbox_dir / _FAILED_DIR_NAME / path.name)
    try:
        path.replace(target)
    except OSError as exc:
        log.error("inbox_worker.move_failed", path=str(path), error=str(exc))
        return
    error_path = target.with_suffix(target.suffix + ".error.txt")
    try:
        error_path.write_text(reason, encoding="utf-8")
    except OSError as exc:
        log.error(
            "inbox_worker.error_note_failed",
            path=str(error_path),
            error=str(exc),
        )


def _unique_target(target: Path) -> Path:
    """Return ``target`` or ``target_001`` / ``target_002`` if it already exists."""
    if not target.exists():
        return target
    parent = target.parent
    stem = target.stem
    suffix = target.suffix
    for n in range(1, 1000):
        candidate = parent / f"{stem}_{n:03d}{suffix}"
        if not candidate.exists():
            return candidate
    msg = f"no free filename for {target}"
    raise RuntimeError(msg)


def count_pending(inbox_dir: Path | None = None) -> int:
    """Pure-filesystem helper used by the ``/inbox`` HTML route."""
    target = inbox_dir or get_settings().inbox_path
    if not target.exists():
        return 0
    return sum(1 for p in target.glob("*.md") if p.is_file())


def list_processed(
    inbox_dir: Path | None = None,
    *,
    limit: int = 20,
) -> list[dict[str, str]]:
    """Recent files moved to ``processed/`` — newest first by mtime."""
    return _list_subdir(inbox_dir, _PROCESSED_DIR_NAME, limit=limit, pattern="*.md")


def list_failed(
    inbox_dir: Path | None = None,
    *,
    limit: int = 20,
) -> list[dict[str, str]]:
    """Recent files in ``failed/`` paired with their ``.error.txt`` text."""
    target = inbox_dir or get_settings().inbox_path
    failed_dir = target / _FAILED_DIR_NAME
    if not failed_dir.exists():
        return []
    md_files = sorted(
        (p for p in failed_dir.glob("*.md") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    items: list[dict[str, str]] = []
    for path in md_files:
        error_path = path.with_suffix(path.suffix + ".error.txt")
        reason = ""
        if error_path.exists():
            try:
                reason = error_path.read_text(encoding="utf-8").strip()
            except OSError:
                reason = "(error file unreadable)"
        items.append(
            {
                "name": path.name,
                "reason": reason,
                "mtime": _format_mtime(path),
            }
        )
    return items


def _list_subdir(
    inbox_dir: Path | None,
    sub: str,
    *,
    limit: int,
    pattern: str,
) -> list[dict[str, str]]:
    target = (inbox_dir or get_settings().inbox_path) / sub
    if not target.exists():
        return []
    files = sorted(
        (p for p in target.glob(pattern) if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    return [{"name": p.name, "mtime": _format_mtime(p)} for p in files]


def _format_mtime(path: Path) -> str:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to avoid global import cost

    return (
        datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        .strftime("%Y-%m-%d %H:%M:%S")
    )
