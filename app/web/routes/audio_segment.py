"""Streaming endpoint for a single ``audio_segment`` row.

v1.11 feature 3/3, route 2 of 5. Companion to
:mod:`app.web.routes.audio_day` — that module emits
``<audio controls src="/audio/segment/{id}">`` against the route below
so the browser's media element can play the segment inline.

Behaviour
---------

``GET /audio/segment/{id}`` resolves the on-disk path stored in
``audio_segment.path``, validates it against ``data_dir`` (defence in
depth — we always *write* a path under that root, but a tampered row
must never escape it), and streams the bytes back with a
``Content-Type`` derived from the row's ``codec`` column.

Four 404 paths are intentional:

1. No row with the requested id.
2. ``path`` is empty / NULL — the row survived a hot-tier retention
   purge that reaped the audio bytes; transcript is still served by
   :mod:`audio_day` but the player has nothing to play.
3. The resolved path falls *outside* ``data_dir`` — should be
   impossible given how we write the row, kept as a paranoid guard so
   a future migration mistake can't turn this endpoint into an
   arbitrary-file reader.
4. The file existed at INSERT time but is gone now (manual rm, disk
   wipe, ...).

All four surface the same opaque ``"not found"`` body — callers don't
need to distinguish, and leaking *which* failure happened would help
an attacker probe the filesystem layout.

This module deliberately does NOT register itself with the FastAPI
app in :mod:`app.web.main` — the task spec forbids touching
``main.py``. Wire it up with::

    from app.web.routes import audio_segment as audio_segment_routes
    app.include_router(audio_segment_routes.router)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

if TYPE_CHECKING:
    from pathlib import Path

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.audio.web")

router = APIRouter(tags=["audio-segment"])

# Mapping from the ``codec`` column to the MIME type we hand the
# browser. Mirrors :data:`app.web.routes.note_attachments._MIME_TO_EXT`
# but pointed the other way (codec → mime). Unknown codecs collapse to
# ``application/octet-stream`` so the player at least sees *something*;
# in practice the worker only writes codecs we've enumerated here.
_CODEC_TO_MIME: Final[dict[str, str]] = {
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "vorbis": "audio/ogg",
    "mp3": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "wav": "audio/wav",
    "wave": "audio/wav",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
}

# Fallback MIME for any codec the worker writes that we don't recognise
# here. The browser will still try to sniff the magic bytes — leaving
# the choice to the UA is safer than guessing wrong.
_FALLBACK_MIME: Final[str] = "application/octet-stream"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mime_for_codec(codec: str | None) -> str:
    """Return the ``Content-Type`` to emit for a given ``codec`` value.

    Normalises the codec string (strip + lower) before lookup so a row
    that stored ``"Opus"`` or ``" opus "`` still resolves cleanly.
    """
    if codec is None:
        return _FALLBACK_MIME
    normalised = codec.strip().lower()
    if not normalised:
        return _FALLBACK_MIME
    return _CODEC_TO_MIME.get(normalised, _FALLBACK_MIME)


def _resolve_segment_path(stored_path: str) -> Path:
    """Resolve ``stored_path`` against ``data_dir`` with a containment check.

    ``stored_path`` is what the database holds (we always write a path
    relative to ``data_dir``, but an absolute path that *happens* to
    sit under ``data_dir`` is also legal). The double-check guarantees
    we never stream a file outside the configured data root even if
    the row was tampered with.
    """
    settings = get_settings()
    candidate = (settings.data_dir / stored_path).resolve()
    data_root = settings.data_dir.resolve()
    try:
        candidate.relative_to(data_root)
    except ValueError as exc:
        log.warning(
            "audio.segment.path_escape_blocked",
            stored_path=stored_path,
            resolved=str(candidate),
        )
        raise HTTPException(status_code=404, detail="not found") from exc
    return candidate


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/audio/segment/{segment_id}")
async def stream_segment(segment_id: int) -> FileResponse:
    """Stream the raw audio bytes for one ``audio_segment`` row.

    The codec column drives the ``Content-Type``; the filename surfaced
    to the browser is fabricated as ``segment-{id}.{ext}`` where
    ``ext`` is the last path component's extension (falls back to
    ``audio`` if the path has none). The disposition is ``inline`` so
    ``<audio controls>`` can play the bytes directly.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, codec, path
              FROM audio_segment
             WHERE id = ?
            """,
            (int(segment_id),),
        )
        row = await cursor.fetchone()

    if row is None:
        log.info("audio.segment.not_found", segment_id=segment_id)
        raise HTTPException(status_code=404, detail="not found")

    raw_path = row["path"]
    stored_path = "" if raw_path is None else str(raw_path).strip()
    if not stored_path:
        # Retention purge has reaped the audio bytes — transcript is
        # still readable through :mod:`audio_day`, but the player has
        # nothing to play. Same 404 surface so the caller can't tell
        # this state apart from a missing row.
        log.info("audio.segment.purged", segment_id=segment_id)
        raise HTTPException(status_code=404, detail="not found")

    resolved = _resolve_segment_path(stored_path)
    if not resolved.exists():
        log.warning(
            "audio.segment.file_missing",
            segment_id=segment_id,
            path=stored_path,
        )
        raise HTTPException(status_code=404, detail="not found")

    mime = _mime_for_codec(row["codec"])
    # Fabricate a friendly download name in case the operator hits
    # "Save As…". Preserve the on-disk extension where possible so the
    # saved file plays without manual renaming.
    suffix = resolved.suffix.lstrip(".") or "audio"
    filename = f"segment-{int(row['id'])}.{suffix}"

    log.info(
        "audio.segment.stream",
        segment_id=int(row["id"]),
        codec=str(row["codec"] or ""),
        mime=mime,
    )
    return FileResponse(
        path=resolved,
        media_type=mime,
        filename=filename,
        # ``inline`` so the ``<audio>`` element renders the bytes
        # straight away; ``filename`` only matters for "Save As…".
        content_disposition_type="inline",
    )


__all__ = ["router"]
