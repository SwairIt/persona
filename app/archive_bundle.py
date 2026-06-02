"""Portable ZIP archive bundle — settings + recent screenshots/notes + thumbs.

This is Persona's lightweight, *unencrypted* sibling of
:mod:`app.backup.snapshot`. The encrypted snapshot is the right tool for
"grandfather" off-site backups; this module is the right tool for the
common "I want to glance at my last week of memory on a different
machine, in a coffee-shop, without typing a passphrase" case.

Layout produced inside the resulting ``.zip``::

    settings.json          — full preference dump (sensitive cols stripped)
    screenshots.json       — every screenshot row from the last N days
    notes.json             — every note (both kinds) from the last N days
    thumbnails/<id>.webp   — one WebP per screenshot id (optional)
    README.txt             — human-readable manifest + restore hints

Hard rules:

* Secrets never enter the archive. Settings come from
  :func:`app.settings_backup.export_settings_json`, which already strips
  ``webhook.secret`` and refuses to export the vault ciphertext.
* All blocking work (``zipfile``, ``open`` for thumbnail reads, ``stat``)
  runs inside :func:`anyio.to_thread.run_sync` so the async route layer
  is never blocked on disk IO.
* The output path is built to disk first (``tempfile`` if the caller
  doesn't pin one). For a 7-day window with thumbnails on, the file is
  typically tens of MiB — too big to hold in a ``BytesIO`` on a small
  laptop.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import anyio

from app.logging_setup import get_logger
from app.settings import get_settings
from app.settings_backup import export_settings_json
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.archive")

# Bumped together with the README template whenever the layout changes
# so an older restore script can refuse a bundle it doesn't understand.
SCHEMA_VERSION: Final[str] = "persona-archive-1"

# Compression level for ``ZIP_DEFLATED``. Level 6 is Python's default —
# we pin it explicitly so a future stdlib change can't silently bloat or
# slow down the archive.
_COMPRESS_LEVEL: Final[int] = 6


async def _dump_screenshots(
    *,
    start_iso: str,
    end_iso: str,
) -> list[dict[str, Any]]:
    """Return every screenshot row whose ``captured_at`` is in ``[start, end)``."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, captured_at, monitor_index, width, height,
                   thumbnail_path, phash, app_name, window_title,
                   process_name, ocr_status, ocr_text,
                   dedup_group_id, created_at
            FROM screenshots
            WHERE captured_at >= ? AND captured_at < ?
            ORDER BY captured_at
            """,
            (start_iso, end_iso),
        )
        rows = await cursor.fetchall()

    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "monitor_index": int(row["monitor_index"]),
            "width": int(row["width"]),
            "height": int(row["height"]),
            "thumbnail_path": (
                str(row["thumbnail_path"]) if row["thumbnail_path"] is not None else None
            ),
            "phash": str(row["phash"]),
            "app_name": (
                str(row["app_name"]) if row["app_name"] is not None else None
            ),
            "window_title": (
                str(row["window_title"]) if row["window_title"] is not None else None
            ),
            "process_name": (
                str(row["process_name"]) if row["process_name"] is not None else None
            ),
            "ocr_status": str(row["ocr_status"]),
            "ocr_text": (
                str(row["ocr_text"]) if row["ocr_text"] is not None else None
            ),
            "dedup_group_id": (
                int(row["dedup_group_id"])
                if row["dedup_group_id"] is not None
                else None
            ),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def _dump_notes(*, start_iso: str, end_iso: str) -> dict[str, Any]:
    """Return both kinds of notes touched inside the window.

    Persona stores two unrelated note tables: ``screenshot_notes`` (one
    body per screenshot) and ``notes`` (standalone inbox markdown).
    The archive carries both so a restore can repopulate either one.

    For ``screenshot_notes`` the window is matched against the parent
    screenshot's ``captured_at`` (which is also why we left-join). For
    standalone ``notes`` we match the note's own ``created_at``.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT n.screenshot_id, n.body, n.created_at, n.updated_at
            FROM screenshot_notes n
            JOIN screenshots s ON s.id = n.screenshot_id
            WHERE s.captured_at >= ? AND s.captured_at < ?
            ORDER BY n.updated_at
            """,
            (start_iso, end_iso),
        )
        screenshot_rows = await cursor.fetchall()
        screenshot_notes = [
            {
                "screenshot_id": int(row["screenshot_id"]),
                "body": str(row["body"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in screenshot_rows
        ]

        # Standalone-notes table is a newer migration; older DBs may
        # lack it. ``IF EXISTS`` keeps the query safe but PRAGMA-checking
        # first lets us return an empty list instead of swallowing an
        # ``OperationalError`` that could hide a real schema problem.
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'"
        )
        has_notes_table = await cursor.fetchone() is not None
        standalone_notes: list[dict[str, Any]] = []
        if has_notes_table:
            cursor = await conn.execute(
                """
                SELECT id, title, body, source, created_at, updated_at
                FROM notes
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at
                """,
                (start_iso, end_iso),
            )
            standalone_rows = await cursor.fetchall()
            standalone_notes = [
                {
                    "id": int(row["id"]),
                    "title": (
                        str(row["title"]) if row["title"] is not None else None
                    ),
                    "body": str(row["body"]),
                    "source": (
                        str(row["source"]) if row["source"] is not None else None
                    ),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in standalone_rows
            ]

    return {
        "screenshot_notes": screenshot_notes,
        "standalone_notes": standalone_notes,
    }


def _render_readme(
    *,
    days: int,
    generated_at: str,
    start_iso: str,
    end_iso: str,
    screenshots_count: int,
    screenshot_notes_count: int,
    standalone_notes_count: int,
    thumbnails_count: int,
    include_thumbnails: bool,
) -> str:
    """Build a short, human-readable manifest for the archive root.

    Kept intentionally plain text (no Markdown) so a non-technical
    recipient who unzips the file on a fresh OS still sees something
    legible in Notepad / TextEdit without rendering tooling.
    """
    thumbs_line = (
        f"Thumbnails:         {thumbnails_count} files in thumbnails/"
        if include_thumbnails
        else "Thumbnails:         (not included; rerun with --thumbs=1 to bundle)"
    )
    return (
        "Persona archive bundle\n"
        "======================\n"
        "\n"
        f"Schema:             {SCHEMA_VERSION}\n"
        f"Generated at (UTC): {generated_at}\n"
        f"Window:             last {days} day(s)\n"
        f"From:               {start_iso}\n"
        f"To (exclusive):     {end_iso}\n"
        "\n"
        f"Screenshots:        {screenshots_count} rows in screenshots.json\n"
        f"Screenshot notes:   {screenshot_notes_count} rows in notes.json\n"
        f"Standalone notes:   {standalone_notes_count} rows in notes.json\n"
        f"{thumbs_line}\n"
        "Settings:           settings.json (preference tables; no secrets)\n"
        "\n"
        "Restore hints\n"
        "-------------\n"
        "* Settings: `persona import-settings --in settings.json` (or POST\n"
        "  the file to /settings/backup/import in the web UI).\n"
        "* Screenshots / notes: the JSON dumps are reference-only — they\n"
        "  are not auto-imported. For a full DB restore use the encrypted\n"
        "  snapshot produced by `persona backup` instead.\n"
        "* Thumbnails: each file is named after its screenshot id; copy\n"
        "  them into `data/thumbnails/<YYYY-MM-DD>/<id>.webp` to wire them\n"
        "  back into the live install.\n"
        "\n"
        "Sensitive material\n"
        "------------------\n"
        "* Webhook secrets are stripped from settings.json.\n"
        "* The vault ciphertext is never included.\n"
        "* Regenerate webhook signing keys after a restore.\n"
    )


def _write_zip(
    *,
    output_path: Path,
    settings_blob: bytes,
    screenshots_blob: bytes,
    notes_blob: bytes,
    readme: str,
    thumbnail_files: list[tuple[int, Path]],
) -> tuple[int, int]:
    """Synchronous worker — writes the ``.zip`` and returns counts.

    Returns ``(thumbnails_written, size_bytes)``. Runs inside
    :func:`anyio.to_thread.run_sync` because every call here is blocking
    IO (``zipfile`` plus per-thumbnail ``read``).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnails_written = 0

    with zipfile.ZipFile(
        output_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=_COMPRESS_LEVEL,
    ) as zf:
        zf.writestr("settings.json", settings_blob)
        zf.writestr("screenshots.json", screenshots_blob)
        zf.writestr("notes.json", notes_blob)

        for screenshot_id, thumb_path in thumbnail_files:
            try:
                zf.write(thumb_path, arcname=f"thumbnails/{screenshot_id}.webp")
                thumbnails_written += 1
            except OSError as exc:
                # A missing-on-disk thumbnail (stale ``thumbnail_path``
                # row) should not blow up the entire export. Log and
                # carry on — the README still reports the final count.
                log.warning(
                    "archive.thumbnail_skipped",
                    screenshot_id=screenshot_id,
                    path=str(thumb_path),
                    error=str(exc),
                )
        zf.writestr("README.txt", readme)

    return thumbnails_written, output_path.stat().st_size


async def build_archive(
    days: int = 7,
    output_path: Path | None = None,
    *,
    include_thumbnails: bool = True,
) -> dict[str, Any]:
    """Build a Persona archive ``.zip`` and return its summary.

    Args:
        days: Lookback window in days, measured from "now (UTC)".
            Must be >= 1.
        output_path: Where to write the archive. Required — callers
            that want a temp path should use :mod:`tempfile` and pass
            the result in. ``None`` raises :class:`ValueError` so we
            never silently drop the artefact in cwd.
        include_thumbnails: When ``True`` (default), every screenshot's
            ``thumbnail_path`` is bundled under ``thumbnails/<id>.webp``.
            When ``False`` the JSON metadata still ships but the
            ``thumbnails/`` prefix is empty.

    Returns:
        ``{"status", "path", "files_count", "size_bytes"}``. ``status``
        is ``"ok"`` on success. ``files_count`` counts every entry the
        zip contains (the three JSON blobs, the README, and one per
        bundled thumbnail).

    Raises:
        ValueError: When ``days < 1`` or ``output_path`` is ``None``.
    """
    if days < 1:
        msg = f"days must be >= 1, got {days}"
        raise ValueError(msg)
    if output_path is None:
        msg = "output_path is required (use tempfile if you don't have a target)"
        raise ValueError(msg)

    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    start_iso = iso(start)
    end_iso = iso(now)

    settings_payload = await export_settings_json()
    screenshots = await _dump_screenshots(start_iso=start_iso, end_iso=end_iso)
    notes_payload = await _dump_notes(start_iso=start_iso, end_iso=end_iso)

    settings_blob = json.dumps(
        settings_payload, ensure_ascii=False, indent=2
    ).encode("utf-8")
    screenshots_blob = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "generated_at": end_iso,
            "from": start_iso,
            "to": end_iso,
            "rows": screenshots,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    notes_blob = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "generated_at": end_iso,
            "from": start_iso,
            "to": end_iso,
            **notes_payload,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    thumbnail_files: list[tuple[int, Path]] = []
    if include_thumbnails:
        settings = get_settings()
        for row in screenshots:
            stored = row["thumbnail_path"]
            candidate: Path | None = None
            if stored:
                stored_path = Path(stored)
                # ``thumbnail_path`` is historically stored as an
                # absolute path, but bundles built with a moved
                # ``data/`` directory will have rows referencing the
                # old location. Fall back to the dated layout below.
                if stored_path.exists():
                    candidate = stored_path
            if candidate is None:
                # Reconstruct the on-disk default: data/thumbnails/
                # <YYYY-MM-DD>/<id>.webp using captured_at.
                try:
                    captured = datetime.fromisoformat(row["captured_at"])
                except ValueError:
                    captured = None
                if captured is not None:
                    fallback = (
                        settings.thumbnails_dir
                        / captured.strftime("%Y-%m-%d")
                        / f"{row['id']}.webp"
                    )
                    if fallback.exists():
                        candidate = fallback
            if candidate is not None:
                thumbnail_files.append((int(row["id"]), candidate))

    screenshot_notes_count = len(notes_payload["screenshot_notes"])
    standalone_notes_count = len(notes_payload["standalone_notes"])
    readme = _render_readme(
        days=days,
        generated_at=end_iso,
        start_iso=start_iso,
        end_iso=end_iso,
        screenshots_count=len(screenshots),
        screenshot_notes_count=screenshot_notes_count,
        standalone_notes_count=standalone_notes_count,
        thumbnails_count=len(thumbnail_files),
        include_thumbnails=include_thumbnails,
    )

    thumbnails_written, size_bytes = await anyio.to_thread.run_sync(
        lambda: _write_zip(
            output_path=output_path,
            settings_blob=settings_blob,
            screenshots_blob=screenshots_blob,
            notes_blob=notes_blob,
            readme=readme,
            thumbnail_files=thumbnail_files,
        )
    )

    # 3 JSON blobs + README + every bundled thumbnail.
    files_count = 4 + thumbnails_written

    log.info(
        "archive.build.ok",
        path=str(output_path),
        days=days,
        screenshots=len(screenshots),
        screenshot_notes=screenshot_notes_count,
        standalone_notes=standalone_notes_count,
        thumbnails=thumbnails_written,
        size_bytes=size_bytes,
        include_thumbnails=include_thumbnails,
    )

    return {
        "status": "ok",
        "path": str(output_path),
        "files_count": files_count,
        "size_bytes": size_bytes,
    }


__all__ = ["SCHEMA_VERSION", "build_archive"]
