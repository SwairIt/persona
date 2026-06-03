"""Share-collection ZIP export — bundle a whole share link as one ``.zip``.

Sibling of :mod:`app.share_collection_pdf`. The PDF surface is the
"printable" face of the same v0.21 share-collection feature; this is
the "machine-readable" face. Where the PDF stitches one page per shot
for a human reviewer, the zip ships the raw thumbnails plus OCR text
plus a manifest so a downstream script (a Jira automation, an
auditor's archive tool, a GDPR data-export pipeline) can consume the
collection without screen-scraping HTML.

Layout produced inside the resulting ``.zip``::

    manifest.json          — schema + collection metadata + per-shot rows
    thumbnails/<id>.webp   — one WebP per resolved shot id
    ocr/<id>.txt           — one UTF-8 text file per shot, with
                             :func:`app.redaction.apply_redaction`
                             applied to the raw ``ocr_text``

Hard rules:

* ``zipfile`` is stdlib, so unlike :mod:`app.share_collection_pdf`
  there is no ``missing_dep`` branch — this surface is always
  available.
* Every blocking call (``zipfile``, ``Path.stat``, per-thumbnail
  ``open``) runs inside :func:`anyio.to_thread.run_sync` so the
  FastAPI event loop never blocks on disk IO while a 50-shot
  collection is being zipped.
* OCR text is passed through :func:`app.redaction.apply_redaction`
  before it lands in the zip — same contract as
  :mod:`app.multi_shot_zip`. The thumbnails stay untouched (they
  already had whatever blur/redaction was applied at capture time),
  but the OCR ``.txt`` siblings must never leak a token or email
  the user has flagged for redaction.
* Missing shots and missing thumbnails are logged and skipped. A
  single stale id must never crash the export — the same FK-free
  contract :mod:`app.share_collection_pdf` already honours.
* Status flags mirror :class:`app.share_collection_pdf.CollectionPdfResult`
  (minus ``missing_dep``) so the HTTP route can branch on the same
  vocabulary as its PDF sibling.
"""

from __future__ import annotations

import json
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypedDict

import anyio.to_thread

from app.logging_setup import get_logger
from app.redaction import apply_redaction
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

log = get_logger("persona.share_collection_zip")

# Bumped together with the layout whenever a new file/field appears so
# an older consumer script can refuse a bundle it doesn't understand.
SCHEMA_VERSION: Final[str] = "persona-share-collection-zip-1"

# Pinned default compression level — Python's default is also 6, but we
# spell it out so a future stdlib change can't silently bloat the bundle.
# Matches :mod:`app.multi_shot_zip` and :mod:`app.archive_bundle`.
_COMPRESS_LEVEL: Final[int] = 6


class CollectionZipResult(TypedDict):
    """Return payload for :func:`build_collection_zip`.

    ``status`` is one of:

    * ``"not_found"`` — no row matches ``slug`` in ``share_collections``.
    * ``"expired"`` — the collection's ``expires_unix`` has passed.
    * ``"corrupt"`` — the stored ``screenshot_ids`` JSON failed to parse.
    * ``"empty"`` — the row exists but resolves to zero live screenshots
      (every id was hard-deleted after the link was minted).
    * ``"ok"`` — the zip was written; ``path`` and ``size_bytes`` are valid.

    Unlike :class:`app.share_collection_pdf.CollectionPdfResult` there is
    no ``"missing_dep"`` — ``zipfile`` is stdlib.
    """

    status: str
    path: str | None
    size_bytes: int
    shots_count: int
    thumbnails_count: int
    ocr_count: int


class _CollectionData(TypedDict):
    """Internal view of one share-collection ready for ZIP rendering."""

    slug: str
    title: str | None
    expires_unix: int
    shots: list[dict[str, Any]]
    earliest: datetime | None
    latest: datetime | None


async def _load_collection(slug: str) -> tuple[str, _CollectionData | None]:
    """Resolve ``slug`` against ``share_collections`` + ``screenshots``.

    Returns ``(status, data)`` so the caller can branch without having
    to re-check status codes. Mirrors
    :func:`app.share_collection_pdf._load_collection` byte-for-byte on
    the status vocabulary so a future refactor can DRY the two if we
    grow a third share-collection surface.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT token, title, screenshot_ids, expires_unix "
            "FROM share_collections WHERE token = ?",
            (slug,),
        )
        row = await cursor.fetchone()
        if row is None:
            return "not_found", None

        expires_unix = int(row["expires_unix"])
        if expires_unix < int(time.time()):
            return "expired", None

        try:
            ids_raw = json.loads(row["screenshot_ids"])
        except (TypeError, ValueError) as exc:
            log.error(
                "share_collection_zip.corrupt_ids",
                slug=slug,
                error=str(exc),
            )
            return "corrupt", None

        # Cast defensively — ``json.loads`` could yield any JSON value
        # if the table was hand-edited. We only accept a list of ints.
        if not isinstance(ids_raw, list):
            log.error(
                "share_collection_zip.corrupt_ids",
                slug=slug,
                kind=type(ids_raw).__name__,
            )
            return "corrupt", None

        shots: list[dict[str, Any]] = []
        for sid_raw in ids_raw:
            try:
                sid = int(sid_raw)
            except (TypeError, ValueError):
                continue
            shot = await get_screenshot(conn, sid)
            if shot is None:
                # v0.14 keeps ``share_collections`` FK-free on purpose;
                # a hard-deleted screenshot simply drops out of the zip.
                continue
            shots.append(
                {
                    "id": shot.id,
                    "captured_at": shot.captured_at,
                    "monitor_index": shot.monitor_index,
                    "width": shot.width,
                    "height": shot.height,
                    "thumbnail_path": shot.thumbnail_path,
                    "app_name": shot.app_name,
                    "window_title": shot.window_title,
                    "ocr_status": shot.ocr_status,
                    "ocr_text": shot.ocr_text,
                }
            )

        title_raw = row["title"]

    earliest: datetime | None = None
    latest: datetime | None = None
    for shot_row in shots:
        captured = shot_row["captured_at"]
        if not isinstance(captured, datetime):
            continue
        if earliest is None or captured < earliest:
            earliest = captured
        if latest is None or captured > latest:
            latest = captured

    data: _CollectionData = {
        "slug": slug,
        "title": (str(title_raw) if title_raw is not None else None),
        "expires_unix": expires_unix,
        "shots": shots,
        "earliest": earliest,
        "latest": latest,
    }
    return "ok", data


def _resolve_thumbnail(shot: dict[str, Any]) -> Path | None:
    """Find the on-disk thumbnail for ``shot`` or return ``None``.

    Matches the fallback strategy used by :mod:`app.multi_shot_zip` —
    the stored ``thumbnail_path`` wins when present on disk, otherwise
    we reconstruct the dated default
    ``thumbnails_dir/<YYYY-MM-DD>/<id>.webp``. A bundle built from a
    moved ``data/`` directory still ships thumbnails when the rows
    reference a stale absolute path.
    """
    stored = shot.get("thumbnail_path")
    if stored:
        stored_path = Path(str(stored))
        if stored_path.is_file():
            return stored_path
        if not stored_path.is_absolute():
            rooted = Path.cwd() / stored_path
            if rooted.is_file():
                return rooted

    captured = shot.get("captured_at")
    if not isinstance(captured, datetime):
        return None

    settings = get_settings()
    fallback = (
        settings.thumbnails_dir
        / captured.strftime("%Y-%m-%d")
        / f"{int(shot['id'])}.webp"
    )
    if fallback.is_file():
        return fallback
    return None


def _format_date_range(
    earliest: datetime | None, latest: datetime | None
) -> str | None:
    """Render ``earliest → latest`` for the manifest.

    Returns ``None`` when both timestamps are missing so the manifest
    drops the key rather than embedding a misleading placeholder.
    Same-day collections collapse to a single date so the manifest
    never reads ``2026-06-01 → 2026-06-01``.
    """
    if earliest is None and latest is None:
        return None
    if earliest is None or latest is None:
        only = earliest or latest
        assert only is not None
        return only.strftime("%Y-%m-%d %H:%M UTC")
    if earliest.date() == latest.date():
        return earliest.strftime("%Y-%m-%d")
    return f"{earliest.strftime('%Y-%m-%d')} → {latest.strftime('%Y-%m-%d')}"


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
                # row) must not blow up the entire export. Log and
                # carry on — the manifest still reports what shipped.
                log.warning(
                    "share_collection_zip.thumbnail_skipped",
                    shot_id=shot_id,
                    path=str(thumb_path),
                    error=str(exc),
                )

        for shot_id, text in ocr_texts:
            zf.writestr(f"ocr/{shot_id}.txt", text.encode("utf-8"))
            ocr_written += 1

    return thumbnails_written, ocr_written, output_path.stat().st_size


def _empty_result(status: str) -> CollectionZipResult:
    """Build a zero-valued :class:`CollectionZipResult` for an error status."""
    return CollectionZipResult(
        status=status,
        path=None,
        size_bytes=0,
        shots_count=0,
        thumbnails_count=0,
        ocr_count=0,
    )


async def build_collection_zip(
    slug: str,
    output_path: Path | str,
) -> CollectionZipResult:
    """Render a ZIP bundle for the share-collection identified by ``slug``.

    Args:
        slug: The signed token minted by the v0.21 share route at
            ``/share/collection/{token}``. Same identifier the public
            viewer and :func:`app.share_collection_pdf.build_collection_pdf`
            accept.
        output_path: Where to write the archive. Required — callers
            that want a temp path should use :mod:`tempfile` and pass
            the result in.

    Returns:
        A :class:`CollectionZipResult`. Branch on ``status``:

        * ``"not_found"`` / ``"expired"`` / ``"corrupt"`` /
          ``"empty"`` — no file was written; ``path`` is ``None``.
        * ``"ok"`` — ``path`` points to ``output_path`` and ``size_bytes``
          is the on-disk size.
    """
    status, data = await _load_collection(slug)
    if status != "ok" or data is None:
        log.info("share_collection_zip.skip", slug=slug, status=status)
        return _empty_result(status)

    if not data["shots"]:
        log.info("share_collection_zip.empty", slug=slug)
        return _empty_result("empty")

    # Apply redaction sequentially — each call is independent but
    # ``apply_redaction`` touches the DB to fetch the rule set, so a
    # tight ``gather()`` would just pile connections up. A collection
    # is bounded by whatever the v0.21 creator UI allows; sequential
    # is fine.
    ocr_texts: list[tuple[int, str]] = []
    for shot in data["shots"]:
        raw = shot.get("ocr_text") or ""
        cleaned, masks = await apply_redaction(str(raw))
        if masks:
            log.info(
                "share_collection_zip.ocr_redacted",
                slug=slug,
                shot_id=int(shot["id"]),
                masks_applied=masks,
            )
        ocr_texts.append((int(shot["id"]), cleaned))

    thumbnail_files: list[tuple[int, Path]] = []
    for shot in data["shots"]:
        thumb = _resolve_thumbnail(shot)
        if thumb is not None:
            thumbnail_files.append((int(shot["id"]), thumb))

    generated_at = datetime.now(UTC).isoformat()
    expires_iso = (
        datetime.fromtimestamp(data["expires_unix"], tz=UTC).isoformat()
    )
    date_range = _format_date_range(data["earliest"], data["latest"])
    has_thumbnail_ids = {sid for sid, _ in thumbnail_files}

    manifest: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at,
        "collection": {
            "slug": data["slug"],
            "title": data["title"],
            "expires_at": expires_iso,
            "date_range": date_range,
            "shots_count": len(data["shots"]),
        },
        "shots": [
            {
                "id": int(shot["id"]),
                "captured_at": (
                    shot["captured_at"].isoformat()
                    if isinstance(shot["captured_at"], datetime)
                    else None
                ),
                "monitor_index": int(shot["monitor_index"]),
                "width": int(shot["width"]),
                "height": int(shot["height"]),
                "app_name": (
                    str(shot["app_name"])
                    if shot["app_name"] is not None
                    else None
                ),
                "window_title": (
                    str(shot["window_title"])
                    if shot["window_title"] is not None
                    else None
                ),
                "ocr_status": str(shot["ocr_status"]),
                "has_thumbnail": int(shot["id"]) in has_thumbnail_ids,
                "thumbnail_filename": (
                    f"thumbnails/{int(shot['id'])}.webp"
                    if int(shot["id"]) in has_thumbnail_ids
                    else None
                ),
                "ocr_filename": f"ocr/{int(shot['id'])}.txt",
            }
            for shot in data["shots"]
        ],
    }
    manifest_blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode(
        "utf-8",
    )

    out_path = Path(output_path)

    # zipfile + per-thumbnail open()s are blocking. Punt the whole
    # synchronous worker into a thread so the FastAPI event loop keeps
    # serving while a 50-shot collection is being zipped.
    thumbnails_written, ocr_written, size_bytes = await anyio.to_thread.run_sync(
        lambda: _write_zip(
            output_path=out_path,
            manifest_blob=manifest_blob,
            thumbnail_files=thumbnail_files,
            ocr_texts=ocr_texts,
        ),
    )

    log.info(
        "share_collection_zip.built",
        slug=slug,
        path=str(out_path),
        shots=len(data["shots"]),
        thumbnails=thumbnails_written,
        ocr_files=ocr_written,
        size_bytes=size_bytes,
    )

    return CollectionZipResult(
        status="ok",
        path=str(out_path),
        size_bytes=size_bytes,
        shots_count=len(data["shots"]),
        thumbnails_count=thumbnails_written,
        ocr_count=ocr_written,
    )


__all__ = ["SCHEMA_VERSION", "CollectionZipResult", "build_collection_zip"]
