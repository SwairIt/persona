"""Multi-shot ZIP share — bundle a hand-picked list of screenshots into one ``.zip``.

The sibling of :mod:`app.archive_bundle`. ``archive_bundle`` answers
"the last N days, everything inside that window"; this module answers
"these specific shot ids — give me a clip I can drop in a Jira ticket".

Layout produced inside the resulting ``.zip``::

    manifest.json          — schema + generated_at + per-shot metadata
    thumbnails/<id>.webp   — one WebP per resolved shot id
    ocr/<id>.txt           — one UTF-8 text file per shot, with
                             :func:`app.redaction.apply_redaction`
                             applied to the raw ``ocr_text``

Hard rules:

* The caller may pass at most :data:`MAX_IDS` ids. Anything beyond is
  rejected with :class:`ValueError`; we never silently truncate because
  the user would not notice that half their selection vanished.
* Every blocking call (``zipfile``, ``open`` for thumbnail reads,
  ``Path.stat``) runs inside :func:`anyio.to_thread.run_sync` so the
  async route layer never blocks on disk IO.
* OCR text is passed through :func:`app.redaction.apply_redaction`
  before it lands in the zip. The original images stay untouched —
  but the ``.txt`` siblings must never leak a token or email that the
  user has flagged for redaction.
* Missing shots and missing thumbnails are logged and skipped. A
  single stale id must never crash the export.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import anyio

from app.logging_setup import get_logger
from app.redaction import apply_redaction
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.multi_shot_zip")

# Bumped together with the layout whenever a new file/field appears so
# an older consumer script can refuse a bundle it doesn't understand.
SCHEMA_VERSION: Final[str] = "persona-multi-shot-zip-1"

# Hard cap. Matches the upper bound the route layer enforces; kept here
# too so direct library callers cannot bypass the route check.
MAX_IDS: Final[int] = 200

# Pinned default compression level — Python's default is also 6, but we
# spell it out so a future stdlib change can't silently bloat the share.
_COMPRESS_LEVEL: Final[int] = 6


def _normalise_ids(shot_ids: list[int]) -> list[int]:
    """Dedup + preserve caller order + reject anything past :data:`MAX_IDS`.

    Order matters: the caller hand-picked these shots in a UI grid and
    expects the manifest's ``shots`` array to come back in that order
    so they can match it back to their selection. We dedup defensively
    in case the UI sent ``[7, 7, 7]``.
    """
    if len(shot_ids) > MAX_IDS:
        msg = f"too many shot ids: got {len(shot_ids)}, max is {MAX_IDS}"
        raise ValueError(msg)

    seen: set[int] = set()
    ordered: list[int] = []
    for raw in shot_ids:
        if raw in seen:
            continue
        seen.add(raw)
        ordered.append(int(raw))
    return ordered


async def _load_shots(shot_ids: list[int]) -> list[dict[str, Any]]:
    """Fetch every requested row in one query, return in caller order.

    Ids that don't resolve are silently dropped — the manifest reflects
    only the shots that actually made it into the zip.
    """
    if not shot_ids:
        return []

    placeholders = ",".join("?" for _ in shot_ids)
    # ``placeholders`` is "?,?,?…" built from the *length* of shot_ids
    # only; the ids themselves are bound parameters below.
    query = (
        "SELECT id, captured_at, monitor_index, width, height, "  # noqa: S608 — static "?" tokens
        "       thumbnail_path, app_name, window_title, ocr_status, ocr_text "
        f"FROM screenshots WHERE id IN ({placeholders})"
    )
    async with get_connection() as conn:
        cursor = await conn.execute(query, tuple(shot_ids))
        rows = await cursor.fetchall()

    by_id: dict[int, dict[str, Any]] = {
        int(row["id"]): {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "monitor_index": int(row["monitor_index"]),
            "width": int(row["width"]),
            "height": int(row["height"]),
            "thumbnail_path": (
                str(row["thumbnail_path"]) if row["thumbnail_path"] is not None else None
            ),
            "app_name": (
                str(row["app_name"]) if row["app_name"] is not None else None
            ),
            "window_title": (
                str(row["window_title"]) if row["window_title"] is not None else None
            ),
            "ocr_status": str(row["ocr_status"]),
            "ocr_text": (
                str(row["ocr_text"]) if row["ocr_text"] is not None else None
            ),
        }
        for row in rows
    }

    # Preserve the caller's ordering. Drop ids that didn't resolve.
    return [by_id[sid] for sid in shot_ids if sid in by_id]


def _resolve_thumbnail(shot: dict[str, Any]) -> Path | None:
    """Find the on-disk thumbnail for ``shot`` or return ``None``.

    Matches the fallback strategy used by :mod:`app.archive_bundle` —
    the stored ``thumbnail_path`` wins when present on disk, otherwise
    we reconstruct the dated default ``thumbnails_dir/<YYYY-MM-DD>/<id>.webp``.
    A bundle built from a moved ``data/`` directory still ships
    thumbnails when the rows reference a stale absolute path.
    """
    stored = shot["thumbnail_path"]
    if stored:
        stored_path = Path(stored)
        if stored_path.exists():
            return stored_path

    captured_raw = shot["captured_at"]
    try:
        captured = datetime.fromisoformat(captured_raw)
    except ValueError:
        return None

    settings = get_settings()
    fallback = (
        settings.thumbnails_dir
        / captured.strftime("%Y-%m-%d")
        / f"{shot['id']}.webp"
    )
    if fallback.exists():
        return fallback
    return None


def _write_zip(
    *,
    output_path: Path,
    manifest_blob: bytes,
    thumbnail_files: list[tuple[int, Path]],
    ocr_texts: list[tuple[int, str]],
) -> tuple[int, int, int]:
    """Synchronous worker — writes the ``.zip`` and returns counts.

    Returns ``(thumbnails_written, ocr_written, size_bytes)``. Runs
    inside :func:`anyio.to_thread.run_sync` because every call here is
    blocking IO (``zipfile`` plus per-thumbnail ``read``).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnails_written = 0
    ocr_written = 0

    with zipfile.ZipFile(
        output_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=_COMPRESS_LEVEL,
    ) as zf:
        zf.writestr("manifest.json", manifest_blob)

        for shot_id, thumb_path in thumbnail_files:
            try:
                zf.write(thumb_path, arcname=f"thumbnails/{shot_id}.webp")
                thumbnails_written += 1
            except OSError as exc:
                # A missing-on-disk thumbnail (stale ``thumbnail_path``
                # row) should not blow up the entire export. Log and
                # carry on — the manifest still reports what shipped.
                log.warning(
                    "multi_shot_zip.thumbnail_skipped",
                    shot_id=shot_id,
                    path=str(thumb_path),
                    error=str(exc),
                )

        for shot_id, text in ocr_texts:
            zf.writestr(f"ocr/{shot_id}.txt", text.encode("utf-8"))
            ocr_written += 1

    return thumbnails_written, ocr_written, output_path.stat().st_size


async def build_shots_zip(
    shot_ids: list[int],
    output_path: Path,
) -> dict[str, Any]:
    """Build a multi-shot share ``.zip`` and return its summary.

    Args:
        shot_ids: Caller-picked screenshot ids. Order is preserved in
            the manifest. Capped at :data:`MAX_IDS`; anything past
            raises :class:`ValueError`. Duplicates are dropped silently.
        output_path: Where to write the archive. Required — callers
            that want a temp path should use :mod:`tempfile` and pass
            the result in.

    Returns:
        ``{"status", "path", "shots_count", "thumbnails_count",
        "ocr_count", "size_bytes"}``. ``status`` is ``"ok"`` on success.
        ``shots_count`` is the number of ids that actually resolved
        (post-dedup, post-DB-lookup); it may be smaller than
        ``len(shot_ids)`` if some rows have been deleted.

    Raises:
        ValueError: When ``shot_ids`` exceeds :data:`MAX_IDS`.
    """
    ordered_ids = _normalise_ids(shot_ids)
    shots = await _load_shots(ordered_ids)

    # Run redaction sequentially — each call is independent but
    # apply_redaction touches the DB to fetch the rule set, so a tight
    # gather() would just pile connections up. The list is at most 200
    # entries long; sequential is fine.
    ocr_texts: list[tuple[int, str]] = []
    for shot in shots:
        raw = shot["ocr_text"] or ""
        cleaned, masks = await apply_redaction(raw)
        if masks:
            log.info(
                "multi_shot_zip.ocr_redacted",
                shot_id=shot["id"],
                masks_applied=masks,
            )
        ocr_texts.append((int(shot["id"]), cleaned))

    thumbnail_files: list[tuple[int, Path]] = []
    for shot in shots:
        thumb = _resolve_thumbnail(shot)
        if thumb is not None:
            thumbnail_files.append((int(shot["id"]), thumb))

    generated_at = datetime.now(UTC).isoformat()
    manifest = {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at,
        "requested_ids": ordered_ids,
        "resolved_count": len(shots),
        "shots": [
            {
                "id": shot["id"],
                "captured_at": shot["captured_at"],
                "monitor_index": shot["monitor_index"],
                "width": shot["width"],
                "height": shot["height"],
                "app_name": shot["app_name"],
                "window_title": shot["window_title"],
                "ocr_status": shot["ocr_status"],
                "has_thumbnail": any(
                    sid == shot["id"] for sid, _ in thumbnail_files
                ),
                "ocr_filename": f"ocr/{shot['id']}.txt",
            }
            for shot in shots
        ],
    }
    manifest_blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode(
        "utf-8",
    )

    thumbnails_written, ocr_written, size_bytes = await anyio.to_thread.run_sync(
        lambda: _write_zip(
            output_path=output_path,
            manifest_blob=manifest_blob,
            thumbnail_files=thumbnail_files,
            ocr_texts=ocr_texts,
        ),
    )

    log.info(
        "multi_shot_zip.build.ok",
        path=str(output_path),
        requested=len(ordered_ids),
        resolved=len(shots),
        thumbnails=thumbnails_written,
        ocr_files=ocr_written,
        size_bytes=size_bytes,
    )

    return {
        "status": "ok",
        "path": str(output_path),
        "shots_count": len(shots),
        "thumbnails_count": thumbnails_written,
        "ocr_count": ocr_written,
        "size_bytes": size_bytes,
    }


__all__ = ["MAX_IDS", "SCHEMA_VERSION", "build_shots_zip"]
