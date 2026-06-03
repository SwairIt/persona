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
    ocr-reindex        Force every shot back to pending (optional --app NAME, --days N).
    capture            Take a single screenshot now (optional --app NAME, --quiet).
    doctor             Run a diagnostic battery (PASS / WARN / FAIL) for triage.
    tag                Bulk-apply a tag to every screenshot matching an FTS5 query.
    untag              Bulk-remove a tag from every screenshot matching an FTS5 query.
    delete             Bulk-delete screenshots matching an FTS5 query (defaults to dry-run).
    pin                Bulk-pin screenshots matching an FTS5 query (defaults to dry-run).
    unpin              Bulk-unpin screenshots matching an FTS5 query.
    lock-shots         Bulk-lock screenshots matching an FTS5 query (defaults to dry-run).
    unlock-shots       Bulk-unlock screenshots matching an FTS5 query (defaults to dry-run).
    export-settings    Dump every preference table to a JSON file (--out FILE).
    import-settings    Insert rows from a settings JSON file (--in FILE [--replace]).
    export-stats-csv   Per-day-per-app rollup CSV (--days N, --out FILE).
    export-monthly-stats-csv  Per-month-per-app rollup CSV (--months N, --out FILE).
    export-share-visits  v0.55 share_visit rows as CSV (--days N, --out FILE).
    export-words-csv   Corpus-wide top-words CSV (--days N --n N, --out FILE).
    export-ocr-txt     Per-day OCR text dump for grep/fzf (--day YYYY-MM-DD, --out FILE).
    export-tag-ocr     Per-tag OCR text dump — every shot carrying a tag (--tag T, --out FILE).
    slack-summary      Compact Slack-style daily summary (--day YYYY-MM-DD, --out FILE).
    export-sticky      Dump every sticky_note row as a JSON array (--out FILE).
    export-annotations-ndjson  Stream every screenshot_annotation row as NDJSON (--out FILE).
    archive            Build a ZIP bundle of recent state (--days N --out FILE [--no-thumbnails]).
    diagnostics-bundle Build a diagnostics ZIP for bug reports (--out FILE; no user data).
    export-collage     Render a 4xN per-day collage PNG (--day YYYY-MM-DD --out FILE).
    tag-orphans        List tag names with zero linked screenshots.
    tag-prune-orphans  Delete every tag with zero linked screenshots.
    tag-untag-old      Remove a tag from shots captured before now-Ndays (--tag NAME --days N).
    thumb-regen        Clear dangling thumbnail_path entries (--limit N).
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
    verify_backup,
)
from app.bulk_delete import bulk_delete
from app.bulk_pin import bulk_pin, bulk_unpin
from app.bulk_tag import bulk_tag, bulk_untag
from app.capture import capture_primary_monitor, get_active_window
from app.day_collage import build_day_collage
from app.dedup import compute_phash, find_or_create_dedup_group
from app.diagnostics import run_doctor
from app.diagnostics_bundle import build_diag_bundle
from app.logging_setup import configure_logging
from app.monthly_stats_csv import export_monthly_stats_csv
from app.ocr_force_reindex import count_candidates, wipe_and_requeue
from app.ocr_txt_export import export_day_ocr_txt
from app.pdf_export import export_day_pdf
from app.search import search as fts_search
from app.settings import get_settings
from app.settings_backup import export_settings_json, import_settings_json
from app.shot_lock_cli import lock_shots, unlock_shots
from app.slack_summary import slack_style_summary
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
from app.tag_cleanup import find_orphan_tags, purge_orphan_tags, untag_older_than
from app.thumb_regen import regen_missing
from app.web.routes.annotations_ndjson import _iter_annotations_ndjson
from app.web.routes.share_visits_csv import _render_share_visits_csv
from app.web.routes.sticky_export import _fetch_all_stickies
from app.web.routes.tag_ocr_export import _build_tag_ocr_dump
from app.web.routes.words_csv import _render_words_csv
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


async def _cmd_ocr_reindex(
    app_filter: str | None,
    days_back: int | None,
    confirm: bool,
) -> int:
    """Force-reset OCR rows back to pending; dry-run unless ``confirm``.

    The dry-run path prints the candidate count and the filter summary so
    the operator can sanity-check the blast radius before re-running with
    ``--confirm``. The destructive path delegates to
    :func:`app.ocr_force_reindex.wipe_and_requeue` and the existing OCR
    worker takes it from there.
    """
    if days_back is not None and days_back < 1:
        print(f"error: --days must be >= 1, got {days_back}", file=sys.stderr)
        return 2
    if app_filter is not None and not app_filter.strip():
        print("error: --app must not be empty", file=sys.stderr)
        return 2

    cleaned_app = app_filter.strip() if app_filter is not None else None
    filter_label = cleaned_app if cleaned_app else "(all apps)"
    window_label = f"last {days_back} day(s)" if days_back is not None else "(all time)"

    if not confirm:
        candidates = await count_candidates(cleaned_app, days_back)
        print(f"App:        {filter_label}")
        print(f"Window:     {window_label}")
        print(f"Candidates: {candidates}")
        print("Re-run with --confirm to wipe OCR status and re-queue.")
        return 0

    affected = await wipe_and_requeue(cleaned_app, days_back)
    print(f"App:      {filter_label}")
    print(f"Window:   {window_label}")
    print(f"Requeued: {affected} row(s) → pending")
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


async def _cmd_verify_backup(src: Path, password: str | None) -> int:
    """Decrypt and inspect an encrypted snapshot without restoring it.

    Mirrors :func:`_cmd_restore`'s password-resolution policy — the
    passphrase comes from ``--password`` or ``PERSONA_BACKUP_PASSWORD``
    and is never echoed back, even on failure.  All work happens against
    a temp directory; the live DB and thumbnails are untouched.
    """
    resolved = _resolve_password(password)
    if not resolved:
        print(
            "error: password required via --password or PERSONA_BACKUP_PASSWORD",
            file=sys.stderr,
        )
        return 2

    try:
        summary = await verify_backup(src, resolved)
    except BackupNotAvailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BackupError as exc:
        # ``exc`` carries our own messages ("wrong password or corrupted
        # backup file", "backup is missing data/persona.db", …) — none of
        # them include the passphrase, so it stays out of the operator's
        # terminal and any log scraper.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Path:        {src}")
    print(f"Status:      {summary['status']}")
    print(f"Files:       {summary['files']}")
    print(f"DB ok:       {'yes' if summary['db_ok'] else 'no'}")
    print(f"Screenshots: {summary['screenshots_count']}")
    return 0 if summary["db_ok"] else 1


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


async def _cmd_diagnostics_bundle(out: Path) -> int:
    """Build a diagnostics ZIP via :func:`app.diagnostics_bundle.build_diag_bundle`."""
    try:
        result = await build_diag_bundle(out)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Path:        {result['path']}")
    print(f"Size bytes:  {result['size_bytes']}")
    return 0


async def _cmd_export_collage(
    day: str | None,
    out: Path,
    cols: int,
    max_shots: int,
) -> int:
    """Render a per-day collage PNG via :func:`app.day_collage.build_day_collage`."""
    # Coalesce both arg checks into a single return so this helper stays
    # under ruff's PLR0911 ceiling (six returns) — the status dispatch
    # below already eats five of the six allowed.
    bad_arg = None
    if cols < 1:
        bad_arg = f"--cols must be >= 1, got {cols}"
    elif max_shots < 1:
        bad_arg = f"--max-shots must be >= 1, got {max_shots}"
    if bad_arg is not None:
        print(f"error: {bad_arg}", file=sys.stderr)
        return 2

    target = _parse_day(day)
    result = await build_day_collage(
        target.isoformat(),
        out,
        cols=cols,
        max_shots=max_shots,
    )

    status = result["status"]
    if status == "bad_date":
        print(f"error: invalid day {target.isoformat()!r}", file=sys.stderr)
        return 2
    if status == "bad_args":
        print("error: invalid collage parameters", file=sys.stderr)
        return 2
    if status == "empty":
        print(
            f"(nothing to export for {target.isoformat()}; no thumbnailed screenshots)",
            file=sys.stderr,
        )
        return 1
    if status != "ok" or result["path"] is None:
        print(f"error: unexpected status {status!r}", file=sys.stderr)
        return 1

    print(f"Day:         {target.isoformat()}")
    print(f"Path:        {result['path']}")
    print(f"Cols:        {result['cols']}")
    print(f"Rows:        {result['rows']}")
    print(f"Tile size:   {result['tile_size']}")
    print(f"Shots used:  {result['shots_used']}")
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


async def _cmd_pin(query: str, limit: int, confirm: bool) -> int:
    """Bulk-pin screenshots matching ``query``; dry-run unless ``confirm``.

    Mirrors :func:`_cmd_delete` — the destructive ``--confirm`` flag
    refuses to run without ``--query`` so a typo cannot accidentally pin
    every shot in the database.
    """
    if confirm and not query.strip():
        print("error: --confirm requires --query", file=sys.stderr)
        return 2
    if not query.strip():
        print("error: empty search query", file=sys.stderr)
        return 2
    if limit < 1:
        print(f"error: --limit must be >= 1, got {limit}", file=sys.stderr)
        return 2

    try:
        result = await bulk_pin(query, limit, dry_run=not confirm)
    except aiosqlite.OperationalError as exc:
        print(f"error: malformed FTS5 query: {exc}", file=sys.stderr)
        return 2

    if result["dry_run"]:
        print(f"Matched {result['matched']} screenshots (dry-run; nothing pinned).")
        preview = result["ids"][:10]
        if preview:
            joined = ", ".join(str(i) for i in preview)
            more = "" if len(result["ids"]) <= 10 else f" (+{len(result['ids']) - 10} more)"
            print(f"First ids: {joined}{more}")
        print("Re-run with --confirm --query ... to pin for real.")
        return 0

    print(f"Pinned {result['pinned']} screenshots.")
    return 0


async def _cmd_unpin(query: str, limit: int) -> int:
    """Bulk-unpin screenshots matching ``query``.

    Un-pinning is non-destructive (the rows drop back to ``hot`` and the
    regular tier sweep handles them), so there is no ``--confirm``
    handshake here — symmetry with the web route, which also skips the
    HMAC preview step for un-pin.
    """
    if not query.strip():
        print("error: empty search query", file=sys.stderr)
        return 2
    if limit < 1:
        print(f"error: --limit must be >= 1, got {limit}", file=sys.stderr)
        return 2

    try:
        result = await bulk_unpin(query, limit)
    except aiosqlite.OperationalError as exc:
        print(f"error: malformed FTS5 query: {exc}", file=sys.stderr)
        return 2

    print(f"Unpinned {result['pinned']} screenshots.")
    return 0


async def _cmd_lock_shots(query: str, limit: int, confirm: bool) -> int:
    """Bulk-lock screenshots matching ``query``; dry-run unless ``confirm``.

    Mirrors :func:`_cmd_delete` / :func:`_cmd_pin` — the destructive
    ``--confirm`` flag refuses to run without ``--query`` so a typo
    cannot accidentally lock every shot in the database.
    """
    if confirm and not query.strip():
        print("error: --confirm requires --query", file=sys.stderr)
        return 2
    if not query.strip():
        print("error: empty search query", file=sys.stderr)
        return 2
    if limit < 1:
        print(f"error: --limit must be >= 1, got {limit}", file=sys.stderr)
        return 2

    try:
        result = await lock_shots(query, limit, dry_run=not confirm)
    except aiosqlite.OperationalError as exc:
        print(f"error: malformed FTS5 query: {exc}", file=sys.stderr)
        return 2

    if result["dry_run"]:
        print(f"Matched {result['matched']} screenshots (dry-run; nothing locked).")
        preview = result["ids"][:10]
        if preview:
            joined = ", ".join(str(i) for i in preview)
            more = "" if len(result["ids"]) <= 10 else f" (+{len(result['ids']) - 10} more)"
            print(f"First ids: {joined}{more}")
        print("Re-run with --confirm --query ... to lock for real.")
        return 0

    print(f"Locked {result['affected']} screenshots.")
    return 0


async def _cmd_unlock_shots(query: str, limit: int, confirm: bool) -> int:
    """Bulk-unlock screenshots matching ``query``; dry-run unless ``confirm``.

    Symmetric with :func:`_cmd_lock_shots` — unlocking strips a guard
    the user explicitly asked for so we keep the same ``--confirm``
    handshake rather than letting a typo silently remove the protection
    from a swathe of shots.
    """
    if confirm and not query.strip():
        print("error: --confirm requires --query", file=sys.stderr)
        return 2
    if not query.strip():
        print("error: empty search query", file=sys.stderr)
        return 2
    if limit < 1:
        print(f"error: --limit must be >= 1, got {limit}", file=sys.stderr)
        return 2

    try:
        result = await unlock_shots(query, limit, dry_run=not confirm)
    except aiosqlite.OperationalError as exc:
        print(f"error: malformed FTS5 query: {exc}", file=sys.stderr)
        return 2

    if result["dry_run"]:
        print(f"Matched {result['matched']} screenshots (dry-run; nothing unlocked).")
        preview = result["ids"][:10]
        if preview:
            joined = ", ".join(str(i) for i in preview)
            more = "" if len(result["ids"]) <= 10 else f" (+{len(result['ids']) - 10} more)"
            print(f"First ids: {joined}{more}")
        print("Re-run with --confirm --query ... to unlock for real.")
        return 0

    print(f"Unlocked {result['affected']} screenshots.")
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


async def _cmd_export_monthly_stats_csv(months: int, out: Path) -> int:
    """Write the per-month-per-app stats CSV produced by ``export_monthly_stats_csv``."""
    if months < 1:
        print(f"error: --months must be >= 1, got {months}", file=sys.stderr)
        return 2

    body = await export_monthly_stats_csv(months_back=months)
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
    print(f"Months: {months}")
    print(f"Rows:   {data_rows}")
    print(f"Bytes:  {size_bytes}")
    return 0


async def _cmd_export_share_visits(days: int, out: Path) -> int:
    """Write the v0.55 ``share_visit`` rows as a CSV via the shared renderer.

    Reuses :func:`app.web.routes.share_visits_csv._render_share_visits_csv`
    so the CLI and the HTTP route always agree on columns, ordering, and
    the ``-N days`` window — there is exactly one query in the codebase
    that materialises this table for offline analysis.
    """
    if days < 1:
        print(f"error: --days must be >= 1, got {days}", file=sys.stderr)
        return 2

    body = await _render_share_visits_csv(days=days)
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


async def _cmd_export_words_csv(days: int, n: int, out: Path) -> int:
    """Write the corpus-wide top-words CSV via the shared renderer.

    Reuses :func:`app.web.routes.words_csv._render_words_csv` so the
    CLI and the HTTP route always agree on tokenisation, the STOPWORDS
    filter, and the look-back window — there is exactly one query in
    the codebase that materialises top words for offline analysis.
    """
    if days < 1:
        print(f"error: --days must be >= 1, got {days}", file=sys.stderr)
        return 2
    if n < 1:
        print(f"error: --n must be >= 1, got {n}", file=sys.stderr)
        return 2

    body = await _render_words_csv(days=days, top_n=n)
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
    print(f"TopN:   {n}")
    print(f"Rows:   {data_rows}")
    print(f"Bytes:  {size_bytes}")
    return 0


async def _cmd_export_sticky(out: Path) -> int:
    """Write every sticky-note row as a JSON array to ``out``.

    Shares :func:`app.web.routes.sticky_export._fetch_all_stickies` with
    the HTTP route so the CLI and the download endpoint always emit the
    same column order and serialisation.
    """
    items = await _fetch_all_stickies()
    body = json.dumps(items, ensure_ascii=False, indent=2)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    size_bytes = len(body.encode("utf-8"))

    print(f"Path:   {out}")
    print(f"Rows:   {len(items)}")
    print(f"Bytes:  {size_bytes}")
    return 0


async def _cmd_export_annotations_ndjson(out: Path) -> int:
    """Stream every ``screenshot_annotation`` row as NDJSON to ``out``.

    Reuses :func:`app.web.routes.annotations_ndjson._iter_annotations_ndjson`
    so the CLI and the HTTP route produce byte-identical files — single
    source of truth for column order, serialisation, and the
    ``screenshot_id → shot_id`` rename. The async generator yields one
    pre-encoded line per row so we write the file row-by-row without
    materialising the whole table in memory.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    size_bytes = 0
    # ``newline=""`` keeps the ``\n`` separators we yield from the
    # generator intact instead of letting the Windows text layer
    # rewrite them to ``\r\n`` — NDJSON consumers expect lone ``\n``.
    with out.open("wb") as fh:
        async for chunk in _iter_annotations_ndjson():
            fh.write(chunk)
            size_bytes += len(chunk)
            rows += 1

    print(f"Path:   {out}")
    print(f"Rows:   {rows}")
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


async def _cmd_export_tag_ocr(tag: str, out: Path) -> int:
    """Write the per-tag OCR text dump via :func:`_build_tag_ocr_dump`.

    Shares the renderer with :mod:`app.web.routes.tag_ocr_export` so the
    CLI and the HTTP route always emit byte-identical files — single
    source of truth for header layout, block delimiter, and OCR
    line-stripping. A missing tag exits non-zero (rc 1) rather than
    silently writing an empty file so shell pipelines fail loudly.
    """
    cleaned_tag = tag.strip().lower()
    if not cleaned_tag:
        print("error: empty tag name", file=sys.stderr)
        return 2

    try:
        tag_row, body = await _build_tag_ocr_dump(cleaned_tag)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    # ``newline=""`` keeps the lone ``\n`` separators emitted by the
    # renderer intact instead of letting the Windows text layer rewrite
    # them to ``\r\n`` — grep / fzf / awk all expect lone ``\n``.
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write(body)

    size_bytes = len(body.encode("utf-8"))
    # Each ``\n===\n`` line separates two blocks; +1 only when the body
    # is non-empty (a tag with zero shots produces a zero-byte file).
    blocks = body.count("\n===\n") + 1 if body else 0

    print(f"Path:   {out}")
    print(f"Tag:    {tag_row['name']}")
    print(f"Blocks: {blocks}")
    print(f"Bytes:  {size_bytes}")
    return 0


async def _cmd_slack_summary(day: str | None, out: Path) -> int:
    """Write the Slack-style daily summary produced by :func:`slack_style_summary`.

    Mirrors ``export-ocr-txt`` in shape — single positional day,
    single ``--out`` path, ``newline=""`` to preserve the lone ``\\n``
    separators the summary helper emits.  The renderer never raises on
    an empty day (it returns placeholder bullets), so there is no
    "nothing to export" branch here.
    """
    target = _parse_day(day)
    try:
        body = await slack_style_summary(target.isoformat())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Trailing newline matches the HTTP route's policy and keeps the
    # on-disk file POSIX-conformant ("a complete line is terminated by
    # a newline") while the renderer itself stays terminator-free.
    payload = body + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    # ``newline=""`` preserves the lone ``\n`` separators we emit
    # instead of letting the Windows text layer rewrite them to
    # ``\r\n`` — paste targets (Slack, Mattermost) expect ``\n``.
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write(payload)

    size_bytes = len(payload.encode("utf-8"))
    line_count = payload.count("\n")

    print(f"Path:   {out}")
    print(f"Day:    {target.isoformat()}")
    print(f"Lines:  {line_count}")
    print(f"Bytes:  {size_bytes}")
    return 0


async def _cmd_tag_orphans() -> int:
    """List the names of tags that have zero rows in ``screenshot_tags``."""
    names = await find_orphan_tags()
    if not names:
        print("(no orphan tags)")
        return 0
    for name in names:
        print(name)
    print(f"\n{len(names)} orphan tag(s).")
    return 0


async def _cmd_tag_prune_orphans() -> int:
    """Delete every tag with zero linked screenshots and report the count."""
    deleted = await purge_orphan_tags()
    print(f"Deleted {deleted} orphan tag(s).")
    return 0


async def _cmd_tag_untag_old(tag_name: str, days: int) -> int:
    """Strip ``tag_name`` from every screenshot older than ``days`` days."""
    cleaned_tag = tag_name.strip().lower()
    if not cleaned_tag:
        print("error: empty tag name", file=sys.stderr)
        return 2
    if days < 0:
        print(f"error: --days must be >= 0, got {days}", file=sys.stderr)
        return 2

    try:
        affected = await untag_older_than(cleaned_tag, days)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"Untagged {affected} screenshot(s) from "
        f"#{cleaned_tag} older than {days} day(s)."
    )
    return 0


async def _cmd_thumb_regen(limit: int) -> int:
    """Scan up to ``limit`` rows and clear dangling ``thumbnail_path`` entries.

    Thin wrapper over :func:`app.thumb_regen.regen_missing` that prints the
    tally the admin route also returns as JSON. ``--limit`` is validated up
    front so a negative or zero value bails before we touch SQLite — the
    underlying helper clamps to 1 defensively, but the operator deserves a
    clearer error than a silent floor-clamp.
    """
    if limit < 1:
        print(f"error: --limit must be >= 1, got {limit}", file=sys.stderr)
        return 2

    result = await regen_missing(limit=limit)
    print(f"Scanned:     {result['scanned']}")
    print(f"Regenerated: {result['regenerated']}")
    print(f"Failed:      {result['failed']}")
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

    ocr_reindex_parser = sub.add_parser(
        "ocr-reindex",
        help=(
            "Force every matching screenshot back to pending so the OCR worker "
            "re-reads it from scratch (dry-run unless --confirm)."
        ),
    )
    ocr_reindex_parser.add_argument(
        "--app",
        dest="app_filter",
        default=None,
        help="Restrict the reset to one app_name (exact match).",
    )
    ocr_reindex_parser.add_argument(
        "--days",
        dest="days_back",
        type=int,
        default=None,
        help="Lookback window in days (default: no time bound).",
    )
    ocr_reindex_parser.add_argument(
        "--confirm",
        dest="confirm",
        action="store_true",
        help="Actually reset (without this flag the command is a dry-run).",
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

    verify_backup_parser = sub.add_parser(
        "verify-backup",
        help=(
            "Decrypt + inspect a backup archive without restoring "
            "(checks manifest, DB integrity, counts entries)."
        ),
    )
    verify_backup_parser.add_argument(
        "--in",
        dest="src",
        type=Path,
        required=True,
        help="Source archive path.",
    )
    verify_backup_parser.add_argument(
        "--password",
        dest="password",
        default=None,
        help="Passphrase (falls back to PERSONA_BACKUP_PASSWORD env var).",
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

    pin_parser = sub.add_parser(
        "pin",
        help=(
            "Bulk-pin screenshots matching an FTS5 query so retention "
            "never demotes them (dry-run unless --confirm)."
        ),
    )
    pin_parser.add_argument(
        "--query",
        dest="query",
        default="",
        help="FTS5 MATCH query — same syntax as `persona search`.",
    )
    pin_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=100,
        help="Maximum number of screenshots to pin (default: 100).",
    )
    pin_parser.add_argument(
        "--confirm",
        dest="confirm",
        action="store_true",
        help="Actually pin (without this flag the command is a dry-run).",
    )

    unpin_parser = sub.add_parser(
        "unpin",
        help=(
            "Bulk-unpin screenshots matching an FTS5 query "
            "(drops them back to the hot tier — non-destructive)."
        ),
    )
    unpin_parser.add_argument(
        "--query",
        dest="query",
        required=True,
        help="FTS5 MATCH query — same syntax as `persona search`.",
    )
    unpin_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=100,
        help="Maximum number of screenshots to unpin (default: 100).",
    )

    lock_shots_parser = sub.add_parser(
        "lock-shots",
        help=(
            "Bulk-lock screenshots matching an FTS5 query so bulk-delete "
            "and recycle skip them (dry-run unless --confirm)."
        ),
    )
    lock_shots_parser.add_argument(
        "--query",
        dest="query",
        default="",
        help="FTS5 MATCH query — same syntax as `persona search`.",
    )
    lock_shots_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=100,
        help="Maximum number of screenshots to lock (default: 100).",
    )
    lock_shots_parser.add_argument(
        "--confirm",
        dest="confirm",
        action="store_true",
        help="Actually lock (without this flag the command is a dry-run).",
    )

    unlock_shots_parser = sub.add_parser(
        "unlock-shots",
        help=(
            "Bulk-unlock screenshots matching an FTS5 query "
            "(re-exposes them to bulk-delete; dry-run unless --confirm)."
        ),
    )
    unlock_shots_parser.add_argument(
        "--query",
        dest="query",
        default="",
        help="FTS5 MATCH query — same syntax as `persona search`.",
    )
    unlock_shots_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=100,
        help="Maximum number of screenshots to unlock (default: 100).",
    )
    unlock_shots_parser.add_argument(
        "--confirm",
        dest="confirm",
        action="store_true",
        help="Actually unlock (without this flag the command is a dry-run).",
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

    monthly_stats_csv_parser = sub.add_parser(
        "export-monthly-stats-csv",
        help=(
            "Per-month-per-app stats rollup CSV "
            "(month, app, shots, active seconds, ocr chars)."
        ),
    )
    monthly_stats_csv_parser.add_argument(
        "--months",
        dest="months",
        type=int,
        default=12,
        help="Lookback window in months (default: 12).",
    )
    monthly_stats_csv_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination CSV path (parent dirs are created).",
    )

    share_visits_parser = sub.add_parser(
        "export-share-visits",
        help=(
            "Dump v0.55 share_visit rows (shot_id, visited_at, ua, "
            "ip_prefix) as a CSV for offline analysis."
        ),
    )
    share_visits_parser.add_argument(
        "--days",
        dest="days",
        type=int,
        default=90,
        help="Lookback window in days (default: 90).",
    )
    share_visits_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination CSV path (parent dirs are created).",
    )

    words_csv_parser = sub.add_parser(
        "export-words-csv",
        help=(
            "Corpus-wide top-words CSV across OCR + notes "
            "(word, count, percent); STOPWORDS filtered."
        ),
    )
    words_csv_parser.add_argument(
        "--days",
        dest="days",
        type=int,
        default=30,
        help="Lookback window in days (default: 30).",
    )
    words_csv_parser.add_argument(
        "--n",
        dest="n",
        type=int,
        default=200,
        help="Maximum number of words to emit (default: 200).",
    )
    words_csv_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination CSV path (parent dirs are created).",
    )

    sticky_parser = sub.add_parser(
        "export-sticky",
        help=(
            "Dump every sticky_note row (id, shot_id, x_pct, y_pct, body, "
            "color, created_at) as a JSON array for offline analysis."
        ),
    )
    sticky_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination JSON path (parent dirs are created).",
    )

    annotations_ndjson_parser = sub.add_parser(
        "export-annotations-ndjson",
        help=(
            "Stream every screenshot_annotation row (id, shot_id, body, "
            "created_at) as newline-delimited JSON for big-data tooling "
            "(jq -c, DuckDB read_ndjson, ClickHouse JSONEachRow)."
        ),
    )
    annotations_ndjson_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination NDJSON path (parent dirs are created).",
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

    tag_ocr_parser = sub.add_parser(
        "export-tag-ocr",
        help=(
            "Per-tag OCR text dump as a flat .txt — one block per shot "
            "carrying the tag, separated by ``===`` (mirrors export-ocr-txt)."
        ),
    )
    tag_ocr_parser.add_argument(
        "--tag",
        dest="tag",
        required=True,
        help="Tag name to export (case-insensitive, whitespace-trimmed).",
    )
    tag_ocr_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination text path (parent dirs are created).",
    )

    slack_summary_parser = sub.add_parser(
        "slack-summary",
        help=(
            "Render a Slack-style daily summary (header + top apps + top "
            "keywords) as a .txt file ready to paste into a chat channel."
        ),
    )
    slack_summary_parser.add_argument(
        "--day",
        dest="day",
        default=None,
        help="Date in YYYY-MM-DD format (default: today).",
    )
    slack_summary_parser.add_argument(
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

    diag_bundle_parser = sub.add_parser(
        "diagnostics-bundle",
        help=(
            "Build a diagnostics ZIP for bug reports — version, doctor, "
            "routes, redacted settings, migrations, recent audit (no user data)."
        ),
    )
    diag_bundle_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination .zip path (parent dirs are created).",
    )

    collage_parser = sub.add_parser(
        "export-collage",
        help="Render a 4xN per-day collage PNG of the day's top thumbnails.",
    )
    collage_parser.add_argument(
        "--day",
        dest="day",
        default=None,
        help="Date in YYYY-MM-DD format (default: today).",
    )
    collage_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        help="Destination PNG path (parent dirs are created).",
    )
    collage_parser.add_argument(
        "--cols",
        dest="cols",
        type=int,
        default=4,
        help="Grid width in tiles (default: 4).",
    )
    collage_parser.add_argument(
        "--max-shots",
        dest="max_shots",
        type=int,
        default=24,
        help="Maximum number of thumbnails to include (default: 24).",
    )

    sub.add_parser(
        "tag-orphans",
        help="List tag names with zero linked screenshots (sorted, one per line).",
    )

    sub.add_parser(
        "tag-prune-orphans",
        help="Delete every tag with zero linked screenshots.",
    )

    untag_old_parser = sub.add_parser(
        "tag-untag-old",
        help="Remove a tag from screenshots captured before now minus N days.",
    )
    untag_old_parser.add_argument(
        "--tag",
        dest="tag_name",
        required=True,
        help="Tag name to detach (case-insensitive, whitespace-trimmed).",
    )
    untag_old_parser.add_argument(
        "--days",
        dest="days",
        type=int,
        required=True,
        help="Cutoff window in days; shots older than this lose the tag.",
    )

    thumb_regen_parser = sub.add_parser(
        "thumb-regen",
        help=(
            "Scan screenshots whose thumbnail_path no longer resolves to a "
            "file on disk and clear the dangling pointer."
        ),
    )
    thumb_regen_parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=500,
        help="Maximum number of rows to scan in one batch (default: 500).",
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
    if args.command == "ocr-reindex":
        return await _cmd_ocr_reindex(args.app_filter, args.days_back, args.confirm)
    if args.command == "capture":
        return await _cmd_capture(args.app, args.quiet)
    if args.command == "doctor":
        return await _cmd_doctor(args.no_color)
    if args.command == "backup":
        return await _cmd_backup(args.out, args.password, args.days)
    if args.command == "restore":
        return await _cmd_restore(args.src, args.password, args.assume_yes)
    if args.command == "verify-backup":
        return await _cmd_verify_backup(args.src, args.password)
    if args.command == "tag":
        return await _cmd_tag(args.tag_name, args.query, args.limit, args.dry_run)
    if args.command == "untag":
        return await _cmd_untag(args.tag_name, args.query, args.limit)
    if args.command == "delete":
        return await _cmd_delete(args.query, args.limit, args.confirm)
    if args.command == "pin":
        return await _cmd_pin(args.query, args.limit, args.confirm)
    if args.command == "unpin":
        return await _cmd_unpin(args.query, args.limit)
    if args.command == "lock-shots":
        return await _cmd_lock_shots(args.query, args.limit, args.confirm)
    if args.command == "unlock-shots":
        return await _cmd_unlock_shots(args.query, args.limit, args.confirm)
    if args.command == "export-settings":
        return await _cmd_export_settings(args.out)
    if args.command == "import-settings":
        return await _cmd_import_settings(args.src, args.replace)
    if args.command == "export-stats-csv":
        return await _cmd_export_stats_csv(args.days, args.out)
    if args.command == "export-monthly-stats-csv":
        return await _cmd_export_monthly_stats_csv(args.months, args.out)
    if args.command == "export-share-visits":
        return await _cmd_export_share_visits(args.days, args.out)
    if args.command == "export-words-csv":
        return await _cmd_export_words_csv(args.days, args.n, args.out)
    if args.command == "export-ocr-txt":
        return await _cmd_export_ocr_txt(args.day, args.out)
    if args.command == "export-tag-ocr":
        return await _cmd_export_tag_ocr(args.tag, args.out)
    if args.command == "slack-summary":
        return await _cmd_slack_summary(args.day, args.out)
    if args.command == "export-sticky":
        return await _cmd_export_sticky(args.out)
    if args.command == "export-annotations-ndjson":
        return await _cmd_export_annotations_ndjson(args.out)
    if args.command == "archive":
        return await _cmd_archive(args.days, args.out, args.include_thumbnails)
    if args.command == "diagnostics-bundle":
        return await _cmd_diagnostics_bundle(args.out)
    if args.command == "export-collage":
        return await _cmd_export_collage(
            args.day, args.out, args.cols, args.max_shots
        )
    if args.command == "tag-orphans":
        return await _cmd_tag_orphans()
    if args.command == "tag-prune-orphans":
        return await _cmd_tag_prune_orphans()
    if args.command == "tag-untag-old":
        return await _cmd_tag_untag_old(args.tag_name, args.days)
    if args.command == "thumb-regen":
        return await _cmd_thumb_regen(args.limit)

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
