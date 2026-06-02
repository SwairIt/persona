"""Standalone Persona CLI — query memory from the terminal without the web app.

Subcommands:
    stats              Summary metrics: screenshots, notes, today's bytes, current streak.
    search QUERY       FTS search across captures.
    export-day [DATE]  Render the journal markdown for a day (default: today).
    export-day-pdf     Render a per-day PDF (--day YYYY-MM-DD, --out FILE).
    export-week-pdf    Render a Mon-Sun weekly PDF (--week YYYY-MM-DD, --out FILE).
    vacuum-db          Run SQLite VACUUM and report the freed bytes.
    ocr-status         OCR pipeline counts (pending / done / skipped / failed).
    reset-ocr          Mass-reset OCR statuses back to pending (--scope skipped|failed|all).
    capture            Take a single screenshot now (optional --app NAME, --quiet).
    doctor             Run a diagnostic battery (PASS / WARN / FAIL) for triage.
    tag                Bulk-apply a tag to every screenshot matching an FTS5 query.
    untag              Bulk-remove a tag from every screenshot matching an FTS5 query.
    delete             Bulk-delete screenshots matching an FTS5 query (defaults to dry-run).
    export-settings    Dump every preference table to a JSON file (--out FILE).
    import-settings    Insert rows from a settings JSON file (--in FILE [--replace]).
    export-stats-csv   Per-day-per-app rollup CSV (--days N, --out FILE).
    export-ocr-txt     Per-day OCR text dump for grep/fzf (--day YYYY-MM-DD, --out FILE).
    archive            Build a ZIP bundle of recent state (--days N --out FILE [--no-thumbnails]).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.analysis import compute_streaks
from app.archive_bundle import build_archive
from app.backup.snapshot import (
    BackupError,
    BackupNotAvailable,
    create_backup,
    restore_backup,
)
from app.bulk_delete import bulk_delete
from app.bulk_tag import bulk_tag, bulk_untag
from app.capture import capture_primary_monitor, get_active_window
from app.dedup import compute_phash, find_or_create_dedup_group
from app.diagnostics import run_doctor
from app.logging_setup import configure_logging
from app.ocr_txt_export import export_day_ocr_txt
from app.pdf_export import export_day_pdf
from app.search import search as fts_search
from app.settings import get_settings
from app.settings_backup import export_settings_json, import_settings_json
from app.stats_csv import export_stats_csv
from app.storage.db import get_connection, init_database
from app.storage.ocr_admin import (
    reset_all_to_pending,
    reset_failed_to_pending,
    reset_skipped_to_pending,
)
from app.storage.repository import insert_screenshot, set_dedup_group_representative
from app.storage.size_log import sample_today, today_bytes
from app.storage.thumbnails import save_thumbnail
from app.storage.time import iso, parse_iso
from app.weekly_pdf import export_week_pdf

_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_RED = "\033[31m"
_ANSI_BOLD = "\033[1m"
_ANSI_RESET = "\033[0m"

_STATUS_COLOURS = {
    "pass": _ANSI_GREEN,
    "warn": _ANSI_YELLOW,
    "fail": _ANSI_RED,
}


def _truncate(text: str, limit: int = 120) -> str:
    """Cut a snippet to a one-liner of at most ``limit`` characters."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


async def _cmd_stats() -> int:
    """Print high-level memory statistics."""
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots")
        screenshots_row = await cursor.fetchone()
        total_screenshots = int(screenshots_row["n"]) if screenshots_row else 0

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshot_notes")
        notes_row = await cursor.fetchone()
        total_notes = int(notes_row["n"]) if notes_row else 0

        settings = get_settings()
        await sample_today(conn, settings.thumbnails_dir)
        bytes_today = await today_bytes(conn)

        streak = await compute_streaks(conn)

    print(f"Screenshots:    {total_screenshots}")
    print(f"Notes:          {total_notes}")
    print(f"Today bytes:    {bytes_today}")
    print(f"Current streak: {streak.current_streak} day(s)")
    print(f"Longest streak: {streak.longest_streak} day(s)")
    print(f"Active 30d:     {streak.active_days_30d}")
    print(f"Active total:   {streak.active_days_total}")
    return 0


async def _cmd_search(query: str) -> int:
    """Run an FTS search and print the top 20 hits."""
    if not query.strip():
        print("error: empty search query", file=sys.stderr)
        return 2
    async with get_connection() as conn:
        hits = await fts_search(conn, query=query, limit=20)

    if not hits:
        print("(no results)")
        return 0

    for hit in hits:
        ts = hit.captured_at.strftime("%Y-%m-%d %H:%M")
        app = hit.app_name or "-"
        title = hit.window_title or "-"
        snippet = _truncate(hit.snippet or "")
        print(f"#{hit.screenshot_id}  {ts}  [{app}] {title}")
        if snippet:
            print(f"    {snippet}")
    print(f"\n{len(hits)} hit(s).")
    return 0


def _parse_day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        msg = f"invalid date: {value!r} (expected YYYY-MM-DD)"
        raise SystemExit(msg) from exc


async def _build_journal_markdown(target: date) -> str | None:
    """Bundle digest + top apps + focus sessions + notes for the day."""
    start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT body, provider FROM daily_digest WHERE day = ?",
            (target.isoformat(),),
        )
        digest_row = await cursor.fetchone()

        cursor = await conn.execute(
            """
            SELECT n.screenshot_id, n.body, n.updated_at,
                   s.app_name, s.window_title, s.captured_at
            FROM screenshot_notes n
            JOIN screenshots s ON s.id = n.screenshot_id
            WHERE s.captured_at >= ? AND s.captured_at < ?
            ORDER BY n.updated_at
            """,
            (iso(start), iso(end)),
        )
        notes = [
            {
                "id": int(row["screenshot_id"]),
                "body": str(row["body"]),
                "updated_at": parse_iso(str(row["updated_at"])),
                "app_name": row["app_name"],
                "window_title": row["window_title"],
            }
            for row in await cursor.fetchall()
        ]

        cursor = await conn.execute(
            """
            SELECT id, started_at, ended_at, duration_minutes, intent, outcome, completed
            FROM focus_sessions
            WHERE DATE(started_at) = ?
            ORDER BY started_at
            """,
            (target.isoformat(),),
        )
        focus = [
            {
                "id": int(row["id"]),
                "started_at": parse_iso(str(row["started_at"])),
                "ended_at": parse_iso(str(row["ended_at"])) if row["ended_at"] else None,
                "duration_minutes": int(row["duration_minutes"]),
                "intent": row["intent"],
                "outcome": row["outcome"],
                "completed": bool(row["completed"]),
            }
            for row in await cursor.fetchall()
        ]

        cursor = await conn.execute(
            """
            SELECT app_name, COUNT(*) AS n FROM screenshots
            WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL
            GROUP BY app_name ORDER BY n DESC LIMIT 8
            """,
            (iso(start), iso(end)),
        )
        top_apps = [(str(row["app_name"]), int(row["n"])) for row in await cursor.fetchall()]

    if not (digest_row or notes or focus or top_apps):
        return None

    lines: list[str] = [f"# Journal — {target.isoformat()}", ""]

    if digest_row:
        lines.append("## AI digest")
        if digest_row["provider"]:
            lines.append(f"_via {digest_row['provider']}_")
        lines.append("")
        lines.append(str(digest_row["body"]).strip())
        lines.append("")

    if top_apps:
        lines.append("## Top apps")
        for app, n in top_apps:
            lines.append(f"- **{app}** — {n} captures")
        lines.append("")

    if focus:
        lines.append("## Focus sessions")
        for session in focus:
            ended = session["ended_at"].strftime("%H:%M") if session["ended_at"] else "—"
            mark = "✓" if session["completed"] else "✗"
            label = session["intent"] or "(no intent)"
            lines.append(
                f"- {mark} **{session['started_at'].strftime('%H:%M')}–{ended}** "
                f"({session['duration_minutes']}m) — {label}"
            )
            if session["outcome"]:
                lines.append(f"  > {session['outcome']}")
        lines.append("")

    if notes:
        lines.append("## Notes")
        for note in notes:
            ts = note["updated_at"].strftime("%H:%M")
            heading = note["app_name"] or "—"
            if note["window_title"]:
                heading += f" — {note['window_title']}"
            lines.append(f"### {ts} — {heading}")
            lines.append(note["body"].rstrip())
            lines.append(f"[screenshot #{note['id']}](/screenshot/{note['id']})")
            lines.append("")

    return "\n".join(lines)


async def _cmd_export_day(date_str: str | None) -> int:
    """Print the day's journal markdown to stdout."""
    target = _parse_day(date_str)
    body = await _build_journal_markdown(target)
    if body is None:
        print(f"(nothing to export for {target.isoformat()})", file=sys.stderr)
        return 1
    print(body)
    return 0


async def _cmd_export_day_pdf(day: str | None, out: Path) -> int:
    """Render a per-day PDF via :func:`app.pdf_export.export_day_pdf`."""
    target = _parse_day(day)
    result = await export_day_pdf(target.isoformat(), out)

    status = result["status"]
    if status == "missing_dep":
        print(
            "error: reportlab is not installed — `uv pip install reportlab` to enable PDF export",
            file=sys.stderr,
        )
        return 1
    if status == "bad_date":
        print(f"error: invalid day {target.isoformat()!r}", file=sys.stderr)
        return 2
    if status == "empty":
        print(
            f"(nothing to export for {target.isoformat()}; no screenshots)",
            file=sys.stderr,
        )
        return 1
    if status != "ok" or result["path"] is None:
        print(f"error: unexpected status {status!r}", file=sys.stderr)
        return 1

    print(f"Day:         {target.isoformat()}")
    print(f"Path:        {result['path']}")
    print(f"Pages:       {result['pages']}")
    print(f"Screenshots: {result['screenshots']}")
    print(f"Notes:       {result['notes']}")
    print(f"Size bytes:  {result['size_bytes']}")
    return 0


def _parse_week(value: str | None) -> date:
    """Parse ``--week`` and snap to that week's Monday (defaults to *this* Mon)."""
    if not value:
        today = date.today()
        return today - timedelta(days=today.weekday())
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        msg = f"invalid week: {value!r} (expected YYYY-MM-DD)"
        raise SystemExit(msg) from exc
    return parsed - timedelta(days=parsed.weekday())


async def _cmd_export_week_pdf(week: str | None, out: Path) -> int:
    """Render a weekly PDF via :func:`app.weekly_pdf.export_week_pdf`."""
    target = _parse_week(week)
    result = await export_week_pdf(target.isoformat(), out)

    status = result["status"]
    if status == "missing_dep":
        print(
            "error: reportlab is not installed — `uv pip install reportlab` to enable PDF export",
            file=sys.stderr,
        )
        return 1
    if status == "bad_date":
        print(f"error: invalid week {target.isoformat()!r}", file=sys.stderr)
        return 2
    if status != "ok" or result["path"] is None:
        print(f"error: unexpected status {status!r}", file=sys.stderr)
        return 1

    print(f"Week start:  {result['week_start']}")
    print(f"Path:        {result['path']}")
    print(f"Pages:       {result['pages']}")
    print(f"Size bytes:  {result['size_bytes']}")
    return 0


async def _cmd_vacuum_db() -> int:
    """Run VACUUM on the SQLite file and report bytes freed."""
    settings = get_settings()
    db_path = settings.db_path
    if not db_path.exists():
        print(f"error: database not found at {db_path}", file=sys.stderr)
        return 1

    before = db_path.stat().st_size
    async with get_connection() as conn:
        await conn.execute("VACUUM")
        await conn.commit()
    after = db_path.stat().st_size
    freed = before - after

    print(f"Path:   {db_path}")
    print(f"Before: {before} bytes")
    print(f"After:  {after} bytes")
    print(f"Freed:  {freed} bytes")
    return 0


async def _cmd_ocr_status() -> int:
    """Print pending / done / skipped / failed OCR counts."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_status, COUNT(*) AS n FROM screenshots GROUP BY ocr_status"
        )
        rows = await cursor.fetchall()
    counts = {str(row["ocr_status"]): int(row["n"]) for row in rows}
    total = sum(counts.values())
    done = counts.get("done", 0)
    pending = counts.get("pending", 0)
    skipped = counts.get("skipped", 0)
    failed = counts.get("failed", 0)
    progress = round(done / total, 3) if total else 0.0

    print(f"Total:    {total}")
    print(f"Done:     {done}")
    print(f"Pending:  {pending}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {failed}")
    print(f"Progress: {progress}")
    return 0


async def _cmd_reset_ocr(scope: str) -> int:
    """Reset ``skipped`` / ``failed`` / both buckets back to ``pending``."""
    async with get_connection() as conn:
        if scope == "skipped":
            affected = await reset_skipped_to_pending(conn)
            label = "skipped"
        elif scope == "failed":
            affected = await reset_failed_to_pending(conn)
            label = "failed"
        elif scope == "all":
            affected = await reset_all_to_pending(conn)
            label = "skipped + failed"
        else:
            print(
                f"error: unknown scope {scope!r} (expected skipped|failed|all)",
                file=sys.stderr,
            )
            return 2

    print(f"Scope:    {label}")
    print(f"Reset:    {affected} row(s) → pending")
    return 0


async def _cmd_capture(app_override: str | None, quiet: bool) -> int:
    """Take a single screenshot now and persist it like the web API does."""
    settings = get_settings()
    try:
        result = await asyncio.to_thread(capture_primary_monitor)
        window = await asyncio.to_thread(get_active_window)
        phash = compute_phash(result.image)

        app_name = app_override if app_override else (window.app_name if window else None)
        window_title = window.title if window else None
        process_name = window.process_name if window else None

        async with get_connection() as conn:
            group_id, _is_new = await find_or_create_dedup_group(
                conn,
                phash=phash,
                now=result.captured_at,
                threshold=settings.dedup_hamming_threshold,
            )
            screenshot_id = await insert_screenshot(
                conn,
                captured_at=result.captured_at,
                width=result.width,
                height=result.height,
                phash=phash,
                monitor_index=result.monitor_index,
                app_name=app_name,
                window_title=window_title,
                process_name=process_name,
                ocr_status="pending" if settings.ocr_enabled else "skipped",
                dedup_group_id=group_id,
            )
            await set_dedup_group_representative(conn, group_id, screenshot_id)

        thumbnail_path = await asyncio.to_thread(
            save_thumbnail,
            result.image,
            result.captured_at,
            screenshot_id,
        )

        async with get_connection() as conn:
            await conn.execute(
                "UPDATE screenshots SET thumbnail_path = ? WHERE id = ?",
                (str(thumbnail_path), screenshot_id),
            )
            await conn.commit()
    except Exception as exc:  # pragma: no cover - surface failures to the shell
        print(f"error: capture failed: {exc}", file=sys.stderr)
        return 1

    if quiet:
        print(screenshot_id)
    else:
        print(f"screenshot_id: {screenshot_id}")
        print(f"app_name:      {app_name or '-'}")
        print(f"captured_at:   {result.captured_at.isoformat()}")
    return 0


def _resolve_password(cli_value: str | None) -> str | None:
    """Return the backup passphrase from CLI flag or PERSONA_BACKUP_PASSWORD."""
    if cli_value:
        return cli_value
    env_value = os.environ.get("PERSONA_BACKUP_PASSWORD")
    if env_value:
        return env_value
    return None


async def _cmd_backup(out: Path, password: str | None, days: int) -> int:
    """Create an encrypted snapshot of the database + recent thumbnails."""
    resolved = _resolve_password(password)
    if not resolved:
        print(
            "error: password required via --password or PERSONA_BACKUP_PASSWORD",
            file=sys.stderr,
        )
        return 2
    if days < 1:
        print(f"error: --days must be >= 1, got {days}", file=sys.stderr)
        return 2

    try:
        summary = await create_backup(out, resolved, days=days)
    except BackupNotAvailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Path:        {summary['path']}")
    print(f"Size bytes:  {summary['size_bytes']}")
    print(f"Screenshots: {summary['screenshots_count']}")
    return 0


async def _cmd_restore(src: Path, password: str | None, assume_yes: bool) -> int:
    """Restore an encrypted snapshot, optionally overwriting the live DB."""
    resolved = _resolve_password(password)
    if not resolved:
        print(
            "error: password required via --password or PERSONA_BACKUP_PASSWORD",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    if settings.db_path.exists() and not assume_yes:
        print(
            f"refusing to overwrite existing database at {settings.db_path}; "
            "re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 1

    try:
        summary = await restore_backup(src, resolved, force=assume_yes)
    except BackupNotAvailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Path:           {summary['path']}")
    print(f"Size bytes:     {summary['size_bytes']}")
    print(f"Screenshots:    {summary['screenshots_count']}")
    print(f"Restored files: {summary['restored_files']}")
    return 0


async def _cmd_export_settings(out: Path) -> int:
    """Write a JSON dump of every preference table to ``out``."""
    payload = await export_settings_json()
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    total = sum(len(rows) for rows in payload["tables"].values())
    print(f"Path:   {out}")
    print(f"Schema: {payload['schema']}")
    print(f"Tables: {len(payload['tables'])}")
    print(f"Rows:   {total}")
    return 0


async def _cmd_import_settings(src: Path, replace: bool) -> int:
    """Insert rows from ``src`` into the local DB (merge by default)."""
    if not src.exists():
        print(f"error: file not found: {src}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        summary = await import_settings_json(payload, merge=not replace)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    mode = "replace" if replace else "merge"
    total = sum(summary.values())
    print(f"Path:   {src}")
    print(f"Mode:   {mode}")
    print(f"Tables: {len(summary)}")
    print(f"Rows:   {total}")
    for table, written in sorted(summary.items()):
        print(f"  - {table}: {written}")
    return 0


async def _cmd_archive(days: int, out: Path, include_thumbnails: bool) -> int:
    """Build a portable ZIP bundle via :func:`app.archive_bundle.build_archive`."""
    if days < 1:
        print(f"error: --days must be >= 1, got {days}", file=sys.stderr)
        return 2

    try:
        result = await build_archive(
            days=days,
            output_path=out,
            include_thumbnails=include_thumbnails,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Path:        {result['path']}")
    print(f"Days:        {days}")
    print(f"Thumbnails:  {'yes' if include_thumbnails else 'no'}")
    print(f"Files:       {result['files_count']}")
    print(f"Size bytes:  {result['size_bytes']}")
    return 0


def _use_colour(no_color: bool) -> bool:
    """Return True only when stdout is a TTY and ``--no-color`` was not passed."""
    if no_color:
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _format_status(status: str, use_colour: bool) -> str:
    """Return a fixed-width 4-char status label, optionally ANSI-coloured."""
    label_map = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    label = label_map.get(status, status.upper())
    if not use_colour:
        return label
    colour = _STATUS_COLOURS.get(status, "")
    return f"{colour}{_ANSI_BOLD}{label}{_ANSI_RESET}"


async def _cmd_doctor(no_color: bool) -> int:
    """Run :func:`app.diagnostics.run_doctor` and print one row per check."""
    use_colour = _use_colour(no_color)
    results = await run_doctor()

    passes = warns = fails = 0
    name_width = max((len(row["name"]) for row in results), default=10)
    for row in results:
        status = row["status"]
        if status == "pass":
            passes += 1
        elif status == "warn":
            warns += 1
        elif status == "fail":
            fails += 1
        label = _format_status(status, use_colour)
        print(f"[{label}] {row['name']:<{name_width}}  {row['detail']}")

    print()
    summary = f"{passes} pass, {warns} warn, {fails} fail"
    print(summary)
    return 0 if fails == 0 else 1


async def _cmd_tag(tag_name: str, query: str, limit: int, dry_run: bool) -> int:
    """Apply ``tag_name`` to every screenshot whose FTS5 MATCH on ``query`` succeeds."""
    cleaned_tag = tag_name.strip().lower()
    if not cleaned_tag:
        print("error: empty tag name", file=sys.stderr)
        return 2
    if not query.strip():
        print("error: empty search query", file=sys.stderr)
        return 2
    if limit < 1:
        print(f"error: --limit must be >= 1, got {limit}", file=sys.stderr)
        return 2

    try:
        result = await bulk_tag(cleaned_tag, query, limit, dry_run)
    except aiosqlite.OperationalError as exc:
        print(f"error: malformed FTS5 query: {exc}", file=sys.stderr)
        return 2

    prefix = "Would tag" if result["dry_run"] else "Tagged"
    print(
        f"{prefix} {result['affected']} screenshots with "
        f"#{result['tag']} (query: {result['query']!r})."
    )
    return 0


async def _cmd_untag(tag_name: str, query: str, limit: int) -> int:
    """Remove ``tag_name`` from every screenshot whose FTS5 MATCH on ``query`` succeeds."""
    cleaned_tag = tag_name.strip().lower()
    if not cleaned_tag:
        print("error: empty tag name", file=sys.stderr)
        return 2
    if not query.strip():
        print("error: empty search query", file=sys.stderr)
        return 2
    if limit < 1:
        print(f"error: --limit must be >= 1, got {limit}", file=sys.stderr)
        return 2

    try:
        result = await bulk_untag(cleaned_tag, query, limit)
    except aiosqlite.OperationalError as exc:
        print(f"error: malformed FTS5 query: {exc}", file=sys.stderr)
        return 2

    print(
        f"Untagged {result['affected']} screenshots from "
        f"#{result['tag']} (query: {result['query']!r})."
    )
    return 0


async def _cmd_delete(query: str, limit: int, confirm: bool) -> int:
    """Bulk-delete screenshots matching ``query``; dry-run unless ``confirm``."""
    if confirm and not query.strip():
        # Refuse the destructive form without a query — protects against
        # `persona-cli delete --confirm` deleting everything.
        print("error: --confirm requires --query", file=sys.stderr)
        return 2
    if not query.strip():
        print("error: empty search query", file=sys.stderr)
        return 2
    if limit < 1:
        print(f"error: --limit must be >= 1, got {limit}", file=sys.stderr)
        return 2

    try:
        result = await bulk_delete(query, limit, dry_run=not confirm)
    except aiosqlite.OperationalError as exc:
        print(f"error: malformed FTS5 query: {exc}", file=sys.stderr)
        return 2

    if result["dry_run"]:
        print(f"Matched {result['matched']} screenshots (dry-run; nothing deleted).")
        preview = result["ids"][:10]
        if preview:
            joined = ", ".join(str(i) for i in preview)
            more = "" if len(result["ids"]) <= 10 else f" (+{len(result['ids']) - 10} more)"
            print(f"First ids: {joined}{more}")
        print("Re-run with --confirm --query ... to delete for real.")
        return 0

    print(f"Deleted {result['deleted']} screenshots.")
    return 0


async def _cmd_export_stats_csv(days: int, out: Path) -> int:
    """Write the per-day-per-app stats CSV produced by :func:`export_stats_csv`."""
    if days < 1:
        print(f"error: --days must be >= 1, got {days}", file=sys.stderr)
        return 2

    body = await export_stats_csv(days_back=days)
    out.parent.mkdir(parents=True, exist_ok=True)
    # ``newline=""`` keeps the stdlib csv-writer line endings (already
    # ``\n``) intact instead of letting the platform layer rewrite them
    # to CRLF on Windows, which would double-up to ``\r\r\n``.
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write(body)

    # Subtract the header line for the user-facing row count.
    line_count = body.count("\n")
    data_rows = max(line_count - 1, 0)
    size_bytes = len(body.encode("utf-8"))

    print(f"Path:   {out}")
    print(f"Days:   {days}")
    print(f"Rows:   {data_rows}")
    print(f"Bytes:  {size_bytes}")
    return 0


async def _cmd_export_ocr_txt(day: str | None, out: Path) -> int:
    """Write the per-day OCR text dump via :func:`export_day_ocr_txt`."""
    target = _parse_day(day)
    try:
        body = await export_day_ocr_txt(target.isoformat())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out.parent.mkdir(parents=True, exist_ok=True)
    # ``newline=""`` keeps the ``\n`` separators emitted by the export
    # function intact instead of letting the Windows text layer rewrite
    # them to ``\r\n`` — grep / fzf / awk all expect lone ``\n``.
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write(body)

    size_bytes = len(body.encode("utf-8"))
    # ``BLOCK_DELIMITER`` lines count blocks-minus-one; +1 only when the
    # body is non-empty (an empty day produces a zero-byte file).
    blocks = body.count("\n===\n") + 1 if body else 0

    print(f"Path:    {out}")
    print(f"Day:     {target.isoformat()}")
    print(f"Blocks:  {blocks}")
    print(f"Bytes:   {size_bytes}")
    return 0


def _build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 — flat subparser table
    parser = argparse.ArgumentParser(
        prog="persona",
        description="Persona — terminal access to your captured memory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="Summary metrics for your memory database.")

    search_parser = sub.add_parser("search", help="Full-text search across captures.")
    search_parser.add_argument("query", help="Search query (FTS5 syntax allowed).")

    export_parser = sub.add_parser(
        "export-day", help="Print the journal markdown for a day."
    )
    export_parser.add_argument(
        "date",
        nargs="?",
        default=None,
        help="Date in YYYY-MM-DD format (default: today).",
    )

    pdf_parser = sub.add_parser(
        "export-day-pdf",
        help="Render a per-day PDF (title + thumbnails + OCR previews + notes).",
    )
    pdf_parser.add_argument(
        "--day",
        dest="day",
        default=None,
        help="Date in YYYY-MM-DD format (default: today).",
    )
    pdf_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination PDF path (parent dirs are created).",
    )

    week_pdf_parser = sub.add_parser(
        "export-week-pdf",
        help="Render a Mon-Sun weekly PDF (heatmap + apps + keywords + streak).",
    )
    week_pdf_parser.add_argument(
        "--week",
        dest="week",
        default=None,
        help="Any date inside the desired week, YYYY-MM-DD (default: this week's Monday).",
    )
    week_pdf_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination PDF path (parent dirs are created).",
    )

    sub.add_parser("vacuum-db", help="Run SQLite VACUUM and report freed bytes.")
    sub.add_parser("ocr-status", help="Show OCR pipeline counts.")

    reset_ocr_parser = sub.add_parser(
        "reset-ocr",
        help="Reset OCR statuses back to pending so the worker re-runs OCR.",
    )
    reset_ocr_parser.add_argument(
        "--scope",
        choices=("skipped", "failed", "all"),
        default="all",
        help="Which bucket to flip back to pending (default: all).",
    )

    capture_parser = sub.add_parser(
        "capture",
        help="Take a single screenshot now (bypasses the capture loop schedule).",
    )
    capture_parser.add_argument(
        "--app",
        dest="app",
        default=None,
        help="Override detected app name (useful when active-window lookup fails).",
    )
    capture_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the integer screenshot id (pipe-friendly).",
    )

    doctor_parser = sub.add_parser(
        "doctor",
        help="Run a diagnostic battery (PASS / WARN / FAIL) and exit 1 if any FAIL.",
    )
    doctor_parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour output (useful for logs and pipes).",
    )

    backup_parser = sub.add_parser(
        "backup",
        help="Create an encrypted snapshot of the database + recent thumbnails.",
    )
    backup_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination archive path (parent dirs are created).",
    )
    backup_parser.add_argument(
        "--password",
        dest="password",
        default=None,
        help="Passphrase (falls back to PERSONA_BACKUP_PASSWORD env var).",
    )
    backup_parser.add_argument(
        "--days",
        dest="days",
        type=int,
        default=30,
        help="Include thumbnails younger than N days (default: 30).",
    )

    restore_parser = sub.add_parser(
        "restore",
        help="Restore an encrypted snapshot produced by `backup`.",
    )
    restore_parser.add_argument(
        "--in",
        dest="src",
        type=Path,
        required=True,
        help="Source archive path.",
    )
    restore_parser.add_argument(
        "--password",
        dest="password",
        default=None,
        help="Passphrase (falls back to PERSONA_BACKUP_PASSWORD env var).",
    )
    restore_parser.add_argument(
        "--yes",
        dest="assume_yes",
        action="store_true",
        help="Skip the overwrite confirmation prompt.",
    )

    tag_parser = sub.add_parser(
        "tag",
        help="Bulk-apply a tag to every screenshot matching an FTS5 query.",
    )
    tag_parser.add_argument(
        "--add",
        dest="tag_name",
        required=True,
        help="Tag name to apply (created on demand, lowercased).",
    )
    tag_parser.add_argument(
        "--query",
        dest="query",
        required=True,
        help="FTS5 MATCH query — same syntax as `persona search`.",
    )
    tag_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=500,
        help="Maximum number of screenshots to tag (default: 500).",
    )
    tag_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Report what would be tagged without writing any rows.",
    )

    untag_parser = sub.add_parser(
        "untag",
        help="Bulk-remove a tag from every screenshot matching an FTS5 query.",
    )
    untag_parser.add_argument(
        "--remove",
        dest="tag_name",
        required=True,
        help="Tag name to remove from matching screenshots.",
    )
    untag_parser.add_argument(
        "--query",
        dest="query",
        required=True,
        help="FTS5 MATCH query — same syntax as `persona search`.",
    )
    untag_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=500,
        help="Maximum number of screenshots to untag (default: 500).",
    )

    delete_parser = sub.add_parser(
        "delete",
        help=(
            "Bulk-delete screenshots matching an FTS5 query "
            "(dry-run unless --confirm)."
        ),
    )
    delete_parser.add_argument(
        "--query",
        dest="query",
        default="",
        help="FTS5 MATCH query — same syntax as `persona search`.",
    )
    delete_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=100,
        help="Maximum number of screenshots to delete (default: 100).",
    )
    delete_parser.add_argument(
        "--confirm",
        dest="confirm",
        action="store_true",
        help="Actually delete (without this flag the command is a dry-run).",
    )

    export_settings_parser = sub.add_parser(
        "export-settings",
        help="Dump every preference table (kv, redaction, webhooks, …) to a JSON file.",
    )
    export_settings_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination JSON path (parent dirs are created).",
    )

    import_settings_parser = sub.add_parser(
        "import-settings",
        help="Insert preference rows from a JSON file produced by export-settings.",
    )
    import_settings_parser.add_argument(
        "--in",
        dest="src",
        type=Path,
        required=True,
        help="Source JSON path.",
    )
    import_settings_parser.add_argument(
        "--replace",
        dest="replace",
        action="store_true",
        help=(
            "Truncate each preference table before inserting "
            "(destructive; default behaviour merges via INSERT OR IGNORE)."
        ),
    )

    stats_csv_parser = sub.add_parser(
        "export-stats-csv",
        help=(
            "Per-day-per-app stats rollup CSV "
            "(date, app, shots, idle/active seconds, ocr chars, has_tldr)."
        ),
    )
    stats_csv_parser.add_argument(
        "--days",
        dest="days",
        type=int,
        default=90,
        help="Lookback window in days (default: 90).",
    )
    stats_csv_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination CSV path (parent dirs are created).",
    )

    ocr_txt_parser = sub.add_parser(
        "export-ocr-txt",
        help=(
            "Per-day OCR text dump as a flat .txt — one block per screenshot, "
            "separated by ``===`` (designed for grep/fzf/awk pipelines)."
        ),
    )
    ocr_txt_parser.add_argument(
        "--day",
        dest="day",
        default=None,
        help="Date in YYYY-MM-DD format (default: today).",
    )
    ocr_txt_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination text path (parent dirs are created).",
    )

    archive_parser = sub.add_parser(
        "archive",
        help=(
            "Build a portable ZIP bundle: settings + last N days of "
            "screenshots/notes + optional thumbnails."
        ),
    )
    archive_parser.add_argument(
        "--days",
        dest="days",
        type=int,
        default=7,
        help="Lookback window in days (default: 7).",
    )
    archive_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination .zip path (parent dirs are created).",
    )
    archive_parser.add_argument(
        "--no-thumbnails",
        dest="include_thumbnails",
        action="store_false",
        help="Skip the thumbnails/ folder (settings + JSON only).",
    )

    return parser


async def _run(args: argparse.Namespace) -> int:  # noqa: PLR0911, PLR0912 — dispatch table
    # Ensure schema + migrations exist before we touch the DB.
    await init_database()

    if args.command == "stats":
        return await _cmd_stats()
    if args.command == "search":
        return await _cmd_search(args.query)
    if args.command == "export-day":
        return await _cmd_export_day(args.date)
    if args.command == "export-day-pdf":
        return await _cmd_export_day_pdf(args.day, args.out)
    if args.command == "export-week-pdf":
        return await _cmd_export_week_pdf(args.week, args.out)
    if args.command == "vacuum-db":
        return await _cmd_vacuum_db()
    if args.command == "ocr-status":
        return await _cmd_ocr_status()
    if args.command == "reset-ocr":
        return await _cmd_reset_ocr(args.scope)
    if args.command == "capture":
        return await _cmd_capture(args.app, args.quiet)
    if args.command == "doctor":
        return await _cmd_doctor(args.no_color)
    if args.command == "backup":
        return await _cmd_backup(args.out, args.password, args.days)
    if args.command == "restore":
        return await _cmd_restore(args.src, args.password, args.assume_yes)
    if args.command == "tag":
        return await _cmd_tag(args.tag_name, args.query, args.limit, args.dry_run)
    if args.command == "untag":
        return await _cmd_untag(args.tag_name, args.query, args.limit)
    if args.command == "delete":
        return await _cmd_delete(args.query, args.limit, args.confirm)
    if args.command == "export-settings":
        return await _cmd_export_settings(args.out)
    if args.command == "import-settings":
        return await _cmd_import_settings(args.src, args.replace)
    if args.command == "export-stats-csv":
        return await _cmd_export_stats_csv(args.days, args.out)
    if args.command == "export-ocr-txt":
        return await _cmd_export_ocr_txt(args.day, args.out)
    if args.command == "archive":
        return await _cmd_archive(args.days, args.out, args.include_thumbnails)

    print(f"error: unknown command {args.command!r}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("(interrupted)", file=sys.stderr)
        return 130
    except SystemExit as exc:
        # _parse_day raises SystemExit with a string message for bad input.
        if isinstance(exc.code, str):
            print(f"error: {exc.code}", file=sys.stderr)
            return 2
        return int(exc.code or 0)


__all__ = ["main"]
