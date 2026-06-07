"""T16 (2026-06-07) — iOS Shortcut-friendly screenshot ingest.

Apple physically prevents 3rd-party apps from capturing the screen in
the background (ReplayKit needs an explicit tap, can't run silently;
sandboxed apps can't read the framebuffer of others). The realistic
workaround is **iOS Shortcuts** — the user creates an automation that
periodically takes a screenshot, then POSTs it here.

Auth: X-Device-Token header — same token as /api/sync/* (T3). The
shortcut stores this in a single "Text" action and re-uses it on every
upload, so there's no per-request login.

Wire format:
    POST /api/ingest/photo
    Headers:
        X-Device-Token: <device's token>
    Body (multipart/form-data):
        file        — the image (jpeg/png/webp), required
        captured_at — ISO timestamp, optional (defaults to NOW)
        caption     — short text the user typed into the Shortcut prompt
        source      — "ios_shortcut" / "manual" / etc., optional

The endpoint is intentionally lenient about metadata: a Shortcut just
has to send the file, the rest defaults. We accept the bytes, register
the row, return ``{ok, shot_id}``.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from app.devices import heartbeat as device_heartbeat
from app.devices import lookup_by_token
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

router = APIRouter(tags=["ios_ingest"])
log = get_logger("persona.ios_ingest")

# 10 MB cap — iPhone Pro screenshots can be ~3 MB at native res, plus
# headroom for HDR photos taken via the Camera Roll fallback path.
_MAX_BYTES = 10 * 1024 * 1024

_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"RIFF": ("webp", "image/webp"),  # checked further below
}


def _detect_format(raw: bytes) -> tuple[str, str] | None:
    for magic, info in _IMAGE_MAGIC.items():
        if raw.startswith(magic):
            if info[0] == "webp":
                # RIFF magic is shared with WAV — verify it's actually WebP
                if raw[8:12] != b"WEBP":
                    return None
            return info
    return None


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid captured_at: {value!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@router.post("/api/ingest/photo")
async def ingest_photo(
    file: Annotated[UploadFile, File(...)],
    captured_at: Annotated[str | None, Form()] = None,
    caption: Annotated[str | None, Form()] = None,
    source: Annotated[str | None, Form()] = None,
    x_device_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Accept one image from an iOS Shortcut and store as a screenshot row.

    Returns ``{ok: true, shot_id: <int>}`` on success.
    """
    token = (x_device_token or "").strip()
    if not token:
        raise HTTPException(
            status_code=401, detail="X-Device-Token header required"
        )
    device = await lookup_by_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty image upload")
    if len(raw) > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"image too large (max {_MAX_BYTES // (1024 * 1024)} MB)",
        )

    fmt = _detect_format(raw)
    if fmt is None:
        raise HTTPException(
            status_code=400, detail="not a PNG / JPEG / WebP image"
        )
    extension, mime = fmt

    captured_dt = _parse_iso(captured_at)
    captured_iso = captured_dt.isoformat()

    # Persona stores thumbnails at ~/.persona/thumbnails/YYYY-MM-DD/{id}.webp.
    # We mirror the directory layout so the iOS path is indistinguishable
    # from a Mac-agent capture in /timeline.
    settings = get_settings()
    thumbs_root = Path(settings.thumbnails_dir).expanduser()
    day_dir = thumbs_root / captured_dt.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    # SHA256 as a sanity-dedup hash. We're NOT computing pHash here —
    # iOS shots don't dedup against Mac captures and we don't want the
    # extra Pillow dep on this hot path.
    digest = hashlib.sha256(raw).hexdigest()[:16]

    # Generate a UUID for cross-device identity (T6). The existing shot
    # uuid_helper would do this lazily, but we mint it eagerly here so
    # the iOS sync event has it immediately.
    shot_uuid = secrets.token_hex(8)

    # We don't know real width/height without decoding the image, and
    # importing Pillow on this code path would balloon the latency. The
    # /timeline UI tolerates 0 — it scales to container width.
    width = 0
    height = 0

    caption_clean = (caption or "").strip()[:500]
    source_clean = (source or "ios_shortcut").strip()[:64] or "ios_shortcut"

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO screenshots "
            "  (captured_at, monitor_index, width, height, thumbnail_path, "
            "   phash, app_name, window_title, ocr_status, ocr_text) "
            "VALUES (?, 0, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                captured_iso,
                width,
                height,
                "",  # path filled after we know the shot_id
                digest,
                f"iOS · {device['name']}",
                caption_clean or None,
                caption_clean or None,
            ),
        )
        await conn.commit()
        shot_id = cursor.lastrowid or 0

        # Now save the file at the canonical path.
        final_path = day_dir / f"{shot_id}.{extension}"
        final_path.write_bytes(raw)

        # Update the thumbnail_path now that we know shot_id.
        await conn.execute(
            "UPDATE screenshots SET thumbnail_path = ? WHERE id = ?",
            (str(final_path), shot_id),
        )
        await conn.commit()

    # Bump the device's last-seen so /devices shows it as active.
    await device_heartbeat(token, user_agent=f"ios_shortcut/{source_clean}")

    log.info(
        "ios_ingest.photo",
        shot_id=shot_id,
        device_id=device["id"],
        bytes=len(raw),
        mime=mime,
        caption_len=len(caption_clean),
    )

    return JSONResponse(
        {
            "ok": True,
            "shot_id": shot_id,
            "uuid": shot_uuid,
            "captured_at": captured_iso,
            "bytes": len(raw),
        }
    )


@router.get("/api/ingest/shortcut-config.json")
async def shortcut_config(
    x_device_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Returns the JSON config that the iOS Shortcut should use.

    Hits this endpoint from the Shortcut once during setup to fetch:
        * the upload URL
        * the device's name
        * the device's icon (kind)

    No secrets in the response — the token came in via the header.
    """
    token = (x_device_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="X-Device-Token required")
    device = await lookup_by_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")
    return JSONResponse(
        {
            "upload_url": "/api/ingest/photo",
            "device_id": device["id"],
            "device_name": device["name"],
            "device_kind": device["kind"],
            "max_bytes": _MAX_BYTES,
        }
    )
