"""Screenshot drag-and-drop import endpoint (v0.94 feature 2/3).

* ``POST /api/import-screenshot`` accepts a multipart PNG/JPEG upload
  (dropped onto :file:`/timeline` by :file:`drag_drop_import.js`),
  persists the original bytes under :file:`data/manual/`, generates a
  thumbnail under :file:`data/thumbnails/YYYY-MM-DD/`, and inserts a
  fresh row in ``screenshots`` with ``app_name='Manual'`` and
  ``ocr_status='pending'`` so the standard OCR worker picks it up on
  its next poll.

Validation is deliberately layered, fastest check first:

1. Byte ceiling (``_MAX_UPLOAD_BYTES`` = 10 MiB) — rejected before any
   decode.
2. Magic-byte sniff (PNG / JPEG) — catches mislabelled ``Content-Type``
   without paying the PIL cost.
3. PIL ``verify`` + dimension probe — catches corrupt files and
   absurd canvases.

Every successful (and every failed) import is recorded in the v0.36
:mod:`app.audit` log under the action ``import_screenshot.upload`` so
the operator can audit who dropped what. The actor field is left
``None`` here — Persona's HTTP surface is loopback-only and the audit
table already records the timestamp, target filename, and byte count.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` (task spec forbids touching ``main.py``). Wire
it up in a follow-up patch with::

    from app.web.routes import import_screenshot as import_screenshot_routes
    app.include_router(import_screenshot_routes.router)
"""

from __future__ import annotations

import io
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import anyio
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError

from app.audit import log_action
from app.dedup import compute_phash, find_or_create_dedup_group
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import (
    insert_screenshot,
    set_dedup_group_representative,
)
from app.storage.thumbnails import save_thumbnail

log = get_logger("persona.import_screenshot")

router = APIRouter(tags=["import-screenshot"])

# 10 MiB hard ceiling on a single drop. Anything larger is almost
# certainly the wrong file or a denial-of-service attempt; refused
# before PIL is invoked.
_MAX_UPLOAD_BYTES: Final[int] = 10 * 1024 * 1024

# Upper bound on pixel dimensions so a 50k x 50k "PNG bomb" cannot
# explode our memory budget when PIL allocates the decode buffer.
_MAX_PIXEL_DIMENSION: Final[int] = 16_384

# Magic-byte prefixes used to fast-reject non-image uploads before
# handing the bytes to PIL. The third byte of ``\xff\xd8\xff`` is the
# JPEG SOI marker followed by the application-specific marker; the
# fourth byte varies (``\xe0`` JFIF, ``\xe1`` Exif, etc.) so we only
# check the first three bytes.
_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC: Final[bytes] = b"\xff\xd8\xff"

# Whitelist of MIME types the browser may legitimately send for a
# screenshot drop. We do not *trust* this value (we still sniff the
# magic bytes), but using it as a coarse pre-filter avoids decoding
# obviously wrong uploads.
_ALLOWED_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/jpg"}
)

# Cap on the original filename we preserve in the manual store. Keeps
# the slug short and avoids OS-level path-length headaches on
# Windows. The actual filename on disk is also prefixed with the
# screenshot id so collisions across drops are impossible.
_MAX_FILENAME_LEN: Final[int] = 80

# Drop everything outside this set so a hostile filename like
# ``../../etc/passwd`` collapses to ``etcpasswd`` before we ever hand
# it to ``Path``. The dot is kept so the extension survives.
_SAFE_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")


def _detect_image_format(raw: bytes) -> str | None:
    """Return ``"png"`` / ``"jpeg"`` if magic bytes match, else ``None``.

    Pure byte-level check — does not invoke PIL, does not allocate.
    Used as the second-layer validation after the content-type pre-
    filter and before we pay the decoder cost.
    """
    if raw.startswith(_PNG_MAGIC):
        return "png"
    if raw.startswith(_JPEG_MAGIC):
        return "jpeg"
    return None


def _validate_image_bytes(raw: bytes, declared_mime: str | None) -> str:
    """Layered validation, returning the detected format (``"png"`` / ``"jpeg"``).

    Raises :class:`fastapi.HTTPException` on any failure with a one-
    line detail message safe to expose to the caller.
    """
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")

    if declared_mime is not None and declared_mime.lower() not in _ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported content type: {declared_mime}",
        )

    detected = _detect_image_format(raw)
    if detected is None:
        raise HTTPException(
            status_code=400,
            detail="not a PNG or JPEG (magic bytes mismatch)",
        )

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(raw)) as decoded:
            width, height = decoded.size
            fmt = (decoded.format or "").lower()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid image: {exc}"
        ) from exc

    # PIL reports ``"JPEG"`` for both ``.jpg`` and ``.jpeg`` files.
    pil_format = "jpeg" if fmt == "jpeg" else fmt
    if pil_format != detected:
        raise HTTPException(
            status_code=400,
            detail=f"format mismatch: magic={detected} pil={pil_format}",
        )
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="image has zero dimension")
    if width > _MAX_PIXEL_DIMENSION or height > _MAX_PIXEL_DIMENSION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"image too large ({width}x{height}); "
                f"max {_MAX_PIXEL_DIMENSION}x{_MAX_PIXEL_DIMENSION}"
            ),
        )
    return detected


def _sanitise_filename(name: str | None, fallback_ext: str) -> str:
    """Return a filesystem-safe, length-bounded filename.

    Strips path separators, normalises odd characters, and guarantees
    an extension matching the detected format. Never produces a name
    starting with ``.`` (so it cannot accidentally collide with a hidden
    file) or longer than :data:`_MAX_FILENAME_LEN`.
    """
    raw_name = (name or "").strip()
    # ``Path(raw_name).name`` strips any path component the browser may
    # have leaked, so ``"../../etc/passwd.png"`` collapses to
    # ``"passwd.png"`` before the regex scrub runs.
    base = Path(raw_name).name
    cleaned = _SAFE_FILENAME_RE.sub("_", base).strip("._")
    if not cleaned:
        cleaned = f"manual.{fallback_ext}"
    elif "." not in cleaned:
        cleaned = f"{cleaned}.{fallback_ext}"
    if len(cleaned) > _MAX_FILENAME_LEN:
        # Preserve the extension when truncating the head.
        stem, dot, ext = cleaned.rpartition(".")
        if dot and ext:
            keep = _MAX_FILENAME_LEN - len(ext) - 1
            cleaned = f"{stem[: max(1, keep)]}.{ext}"
        else:
            cleaned = cleaned[:_MAX_FILENAME_LEN]
    return cleaned


def _manual_dir() -> Path:
    """Resolve and ensure :file:`<data_dir>/manual/` exists.

    The directory is *not* created at import time because
    :func:`get_settings` lazily resolves the data root from the
    environment — we wait until the first drop to materialise the
    folder so unit tests that override ``PERSONA_DATA_DIR`` are not
    surprised by a side-effect at import.
    """
    settings = get_settings()
    out = settings.data_dir / "manual"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_original_bytes(target: Path, raw: bytes) -> None:
    """Write ``raw`` to ``target`` synchronously (called via thread).

    Kept as a tiny named helper so the ``anyio.to_thread`` call below
    reads cleanly and a future change (chunked write, fsync, ...) has
    one obvious place to land.
    """
    target.write_bytes(raw)


@router.post("/api/import-screenshot")
async def import_screenshot(
    file: Annotated[UploadFile, File(...)],
) -> JSONResponse:
    """Accept a dropped PNG/JPEG and import it as a manual screenshot.

    Pipeline:

    1. Validate size, MIME, and magic bytes.
    2. Compute pHash on the in-memory copy (dedup against any existing
       group so the operator can spot they dropped the same picture
       twice).
    3. ``INSERT INTO screenshots`` with ``app_name='Manual'`` and
       ``ocr_status='pending'`` — the OCR worker drains pending rows
       on its next poll.
    4. Persist the original bytes under :file:`data/manual/<id>-<name>`.
    5. Save a thumbnail under the dated thumbnails folder (so the
       timeline shows the drop just like a real capture).
    6. ``UPDATE screenshots SET thumbnail_path = ?``.
    7. Audit-log the action with action ``import_screenshot.upload``.
    """
    raw = await file.read()
    declared_mime = (file.content_type or None) if file is not None else None
    detected = _validate_image_bytes(raw, declared_mime)

    safe_name = _sanitise_filename(file.filename, detected)
    captured_at = datetime.now(tz=UTC)

    # PIL needs a fresh BytesIO per ``Image.open`` because ``verify``
    # consumes the stream. The pHash + thumbnail steps reuse the same
    # decoded copy to avoid decoding the bytes three times.
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            phash = compute_phash(image)
            width, height = image.size
            # Save a defensive copy so the original ``image`` (whose
            # ``fp`` is closed once we leave the ``with`` block) does
            # not blow up the later ``save_thumbnail`` call.
            decoded_copy = image.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        # Should not happen — ``_validate_image_bytes`` already
        # decoded the bytes successfully — but guard so a corrupted
        # in-memory buffer cannot 500 the route.
        log.warning("import_screenshot.decode_failed", error=str(exc))
        raise HTTPException(status_code=400, detail=f"decode failed: {exc}") from exc

    settings = get_settings()

    async with get_connection() as conn:
        group_id, _is_new = await find_or_create_dedup_group(
            conn,
            phash=phash,
            now=captured_at,
            threshold=settings.dedup_hamming_threshold,
        )
        screenshot_id = await insert_screenshot(
            conn,
            captured_at=captured_at,
            width=width,
            height=height,
            phash=phash,
            monitor_index=0,
            app_name="Manual",
            window_title=safe_name,
            process_name=None,
            ocr_status="pending",
            dedup_group_id=group_id,
        )
        await set_dedup_group_representative(conn, group_id, screenshot_id)

    # Persist the original bytes after the row exists so the filename
    # can be deterministically prefixed with the row id — no collisions
    # even when two drops share the same source filename.
    manual_dir = _manual_dir()
    # Belt-and-braces: if a sibling write somehow lands first, fall
    # back to a short random suffix rather than overwriting an
    # existing file.
    target = manual_dir / f"{screenshot_id}-{safe_name}"
    if target.exists():
        suffix = secrets.token_hex(4)
        target = manual_dir / f"{screenshot_id}-{suffix}-{safe_name}"
    await anyio.to_thread.run_sync(_write_original_bytes, target, raw)

    # Thumbnail generation uses the standard pipeline so the timeline
    # renders the manual import exactly like a real capture.
    thumbnail_path = await anyio.to_thread.run_sync(
        save_thumbnail,
        decoded_copy,
        captured_at,
        screenshot_id,
    )

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE screenshots SET thumbnail_path = ? WHERE id = ?",
            (str(thumbnail_path), screenshot_id),
        )
        await conn.commit()

    log.info(
        "import_screenshot.ok",
        screenshot_id=screenshot_id,
        bytes=len(raw),
        width=width,
        height=height,
        format=detected,
        filename=safe_name,
        manual_path=str(target),
        thumbnail_path=str(thumbnail_path),
    )
    await log_action(
        action="import_screenshot.upload",
        target=str(screenshot_id),
        detail=(
            f"filename={safe_name} bytes={len(raw)} "
            f"format={detected} {width}x{height}"
        ),
        success=True,
    )

    return JSONResponse(
        {
            "ok": True,
            "screenshot_id": screenshot_id,
            "captured_at": captured_at.isoformat(),
            "width": width,
            "height": height,
            "format": detected,
            "filename": safe_name,
        },
        status_code=201,
    )


__all__ = ["router"]
