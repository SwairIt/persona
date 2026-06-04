"""Inline waveform player for ``audio_segment`` rows.

Three routes, one Jinja partial — together they give the timeline UI a
self-contained playable widget per segment without having to bake the
``<audio>`` tag into the day view template directly.

Routes
------

* ``GET /api/audio-segment/{seg_id}/waveform.json`` — computes per-bucket
  peak amplitudes via :func:`app.audio.waveform.compute_waveform_peaks`
  and returns ``{"peaks": [...], "duration_seconds": <float>}``. Peaks
  are cached in a module-level dict so the (mildly expensive) decode
  step runs at most once per segment per process lifetime.
* ``GET /audio-segment/{seg_id}/stream`` — streams the on-disk file with
  a ``Content-Type`` derived from the file extension (``audio/opus`` for
  ``.opus``, ``audio/wav`` for ``.wav``, fall through to
  ``application/octet-stream`` so the browser still sniffs the magic
  bytes for any unknown codec the worker might one day write).
* ``GET /audio-segment/{seg_id}/player`` — minimal HTML fragment
  embedding both the SVG waveform and the ``<audio controls>`` element.
  Rendered from :file:`_audio_player.html`, returned as
  ``HTMLResponse`` so an HTMX call can swap the result straight into a
  container without the surrounding ``base.html`` chrome.

The streaming route deliberately uses a different URL prefix
(``/audio-segment/...``) from the existing :mod:`audio_segment` module
(``/audio/segment/{id}``) so the two coexist — the older endpoint is
referenced from the day view, the newer one is used by the inline
player widget. Same row, two consumer paths.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — the task spec forbids touching ``main.py``.
Wire it up with::

    from app.web.routes import audio_player as audio_player_routes
    app.include_router(audio_player_routes.router)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.audio.waveform import compute_waveform_peaks
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger("persona.audio.player")

router = APIRouter(tags=["audio-player"])

# Extension → MIME mapping for the streaming endpoint. The spec calls
# out ``audio/opus`` and ``audio/wav`` explicitly; anything else falls
# back to ``application/octet-stream`` so the browser still sniffs the
# bytes rather than mis-labelling them.
_EXT_TO_MIME: Final[dict[str, str]] = {
    ".opus": "audio/opus",
    ".wav": "audio/wav",
}
_FALLBACK_MIME: Final[str] = "application/octet-stream"

# Default waveform resolution. Matches the spec; small enough that the
# JSON payload stays under 4 kB and the SVG renders crisply at the
# 100%-width / 60px-tall canvas.
_DEFAULT_PEAK_SAMPLES: Final[int] = 200

# Module-level peak cache. Decoding even a 30-second Opus chunk costs
# ~50ms via ``soundfile``; caching keyed by ``seg_id`` makes the second
# render free. Capped at 200 entries — when we hit the cap the cache is
# cleared wholesale (a simple bounded eviction beats wiring up an
# OrderedDict-LRU here, since the per-day audio view rarely exceeds a
# few hundred segments and process restarts already drop the cache).
_PEAK_CACHE_LIMIT: Final[int] = 200
_peak_cache: dict[int, list[float]] = {}


# ---------------------------------------------------------------------------
# Row resolution helpers
# ---------------------------------------------------------------------------


async def _load_segment(seg_id: int) -> tuple[str, float] | None:
    """Fetch ``(path, duration_seconds)`` for ``seg_id`` or ``None`` if missing.

    Parametrised SQL — the only user-controlled value (``seg_id``) is
    bound, never interpolated. Returns ``None`` for the no-row case so
    callers can collapse "row missing" and "row purged" into one 404
    surface (matching :mod:`audio_segment`).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT path, duration_seconds
              FROM audio_segment
             WHERE id = ?
            """,
            (int(seg_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    raw_path = row["path"]
    stored = "" if raw_path is None else str(raw_path).strip()
    duration_raw = row["duration_seconds"]
    try:
        duration = float(duration_raw) if duration_raw is not None else 0.0
    except (TypeError, ValueError):
        duration = 0.0
    return stored, duration


def _resolve_path(stored_path: str) -> Path | None:
    """Resolve ``stored_path`` under ``data_dir`` with a containment guard.

    The DB always stores paths under ``data_dir`` but a tampered row
    must not be able to coax this endpoint into reading arbitrary files.
    Returns ``None`` when the resolution falls outside the data root —
    callers translate that into a 404 (same opaque surface as the
    "row missing" branch).
    """
    if not stored_path:
        return None
    settings = get_settings()
    candidate = (settings.data_dir / stored_path).resolve()
    data_root = settings.data_dir.resolve()
    try:
        candidate.relative_to(data_root)
    except ValueError:
        log.warning(
            "audio.player.path_escape_blocked",
            stored_path=stored_path,
            resolved=str(candidate),
        )
        return None
    return candidate


def _mime_for_path(path: Path) -> str:
    """Return the ``Content-Type`` for ``path`` based on its extension."""
    return _EXT_TO_MIME.get(path.suffix.lower(), _FALLBACK_MIME)


def _cached_peaks(seg_id: int, audio_path: Path) -> list[float]:
    """Return cached peaks for ``seg_id``, computing + storing on miss.

    Cache eviction policy is intentionally simple: when the cache hits
    :data:`_PEAK_CACHE_LIMIT` entries we clear it wholesale. The peak
    computation is cheap enough to rebuild and the cache is per-process,
    so a restart already cycles it.
    """
    cached = _peak_cache.get(seg_id)
    if cached is not None:
        return cached
    peaks = compute_waveform_peaks(audio_path, samples=_DEFAULT_PEAK_SAMPLES)
    if len(_peak_cache) >= _PEAK_CACHE_LIMIT:
        log.info("audio.player.peak_cache_cleared", size=len(_peak_cache))
        _peak_cache.clear()
    _peak_cache[seg_id] = peaks
    return peaks


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/audio-segment/{seg_id}/waveform.json")
async def waveform_json(seg_id: int) -> JSONResponse:
    """Return ``{peaks, duration_seconds}`` for the inline SVG waveform.

    The peaks list is normalised into ``[0.0, 1.0]`` and capped at the
    default sample count (see :data:`_DEFAULT_PEAK_SAMPLES`). On any
    failure path (row missing, file missing, ``soundfile`` not
    installed) we still return ``200`` with ``peaks=[]`` so the
    JavaScript caller can fall back to the audio-only player without
    error handling.
    """
    loaded = await _load_segment(seg_id)
    if loaded is None:
        log.info("audio.player.waveform.not_found", seg_id=seg_id)
        raise HTTPException(status_code=404, detail="not found")
    stored_path, duration = loaded
    resolved = _resolve_path(stored_path)
    if resolved is None or not resolved.exists():
        log.info(
            "audio.player.waveform.file_missing",
            seg_id=seg_id,
            stored_path=stored_path,
        )
        return JSONResponse({"peaks": [], "duration_seconds": duration})
    peaks = _cached_peaks(seg_id, resolved)
    log.info(
        "audio.player.waveform.ok",
        seg_id=seg_id,
        peaks=len(peaks),
        duration_seconds=duration,
    )
    return JSONResponse({"peaks": peaks, "duration_seconds": duration})


@router.get("/audio-segment/{seg_id}/stream")
async def stream_segment(seg_id: int) -> FileResponse:
    """Stream the raw audio bytes for ``seg_id`` to an ``<audio>`` element.

    Mirrors the older :mod:`audio_segment` streamer but lives under a
    different prefix (``/audio-segment/...``) and derives the MIME type
    from the file *extension* rather than the ``codec`` column — the
    waveform player only ever streams the two formats the encoder writes
    (``.opus`` / ``.wav``) and the file suffix is the truth source for
    them.
    """
    loaded = await _load_segment(seg_id)
    if loaded is None:
        log.info("audio.player.stream.not_found", seg_id=seg_id)
        raise HTTPException(status_code=404, detail="not found")
    stored_path, _duration = loaded
    resolved = _resolve_path(stored_path)
    if resolved is None or not resolved.exists():
        log.info(
            "audio.player.stream.file_missing",
            seg_id=seg_id,
            stored_path=stored_path,
        )
        raise HTTPException(status_code=404, detail="not found")
    mime = _mime_for_path(resolved)
    suffix = resolved.suffix.lstrip(".") or "audio"
    log.info(
        "audio.player.stream.ok",
        seg_id=seg_id,
        mime=mime,
        ext=suffix,
    )
    return FileResponse(
        path=resolved,
        media_type=mime,
        filename=f"segment-{seg_id}.{suffix}",
        content_disposition_type="inline",
    )


@router.get("/audio-segment/{seg_id}/player", response_class=HTMLResponse)
async def player_fragment(seg_id: int, request: Request) -> HTMLResponse:
    """Render the inline waveform-player fragment for HTMX embedding.

    The returned HTML is a single ``<div>`` containing an inline SVG
    waveform and an ``<audio controls preload="none">`` element. No
    ``base.html`` chrome — the caller swaps the fragment straight into
    a host container via ``hx-get`` / ``hx-target``.
    """
    loaded = await _load_segment(seg_id)
    if loaded is None:
        log.info("audio.player.fragment.not_found", seg_id=seg_id)
        raise HTTPException(status_code=404, detail="not found")
    stored_path, duration = loaded
    resolved = _resolve_path(stored_path)
    if resolved is None or not resolved.exists():
        # Render the fragment anyway — empty peaks, empty audio src is
        # not useful so we surface a discreet "audio purged" hint. Same
        # template handles both populated and empty rows so the host
        # container does not have to special-case its layout.
        log.info(
            "audio.player.fragment.file_missing",
            seg_id=seg_id,
            stored_path=stored_path,
        )
        return templates.TemplateResponse(
            request,
            "_audio_player.html",
            {
                "seg": {"id": seg_id, "duration_seconds": duration},
                "peaks": [],
                "has_audio": False,
            },
        )
    peaks = _cached_peaks(seg_id, resolved)
    log.info(
        "audio.player.fragment.ok",
        seg_id=seg_id,
        peaks=len(peaks),
        duration_seconds=duration,
    )
    return templates.TemplateResponse(
        request,
        "_audio_player.html",
        {
            "seg": {"id": seg_id, "duration_seconds": duration},
            "peaks": peaks,
            "has_audio": True,
        },
    )


__all__ = ["router"]
