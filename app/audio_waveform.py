"""Pre-rendered SVG waveform thumbnails for ``audio_segment`` rows.

Companion to the live :mod:`app.audio.waveform` peak extractor — that
module decodes on every HTTP hit, which is fine for the inline
``<audio>`` player but visibly stalls a 50-row timeline page. This
module is the **cache producer**: a background worker
(:mod:`app.workers.audio_waveform_worker`) walks rows whose
``waveform_svg`` column is still NULL, calls
:func:`generate_waveform`, and stores a rendered SVG sparkline in
the row. List templates can then ``{{ seg.waveform_svg|safe }}``
straight onto the page with zero filesystem cost on the hot path.

Decoder priority
----------------

The decoder is intentionally minimal:

1. **``wave`` (stdlib)** — covers the ``.wav`` files the worker writes
   when Opus is unavailable, plus any operator-provided WAV captures.
2. **``aifc`` (stdlib)** — same family, covers ``.aiff`` /  ``.aifc``
   captures from non-Persona producers.
3. **``soundfile`` (optional dep)** — last resort for the ``.opus``
   files the encoder usually writes. Imported lazily — a vanilla
   install without ``libsndfile`` still gets sparklines for WAV
   captures.

When all three paths fail (no decoder, unreadable bytes, …) the
function returns ``{"status": "error"}`` / ``{"status": "missing"}``
and writes nothing — the worker will skip the row and retry on the
next cycle once the operator installs the missing dependency or
restores the file.

SVG shape
---------

* Width is ``bars * 4`` pixels (3 px bar + 1 px gap), height is
  ``height`` pixels (24 by default — matches the 24-px row height of
  the timeline list).
* Each bucket renders one ``<rect>`` anchored at the vertical centre,
  so silent buckets render as a 1-px baseline (visually distinguishes
  "no audio" from "no waveform pre-rendered").
* Bars are filled with ``currentColor`` so the template owner controls
  the colour via CSS — no hard-coded palette pollution.
* The SVG is namespaced and self-contained — safe for ``|safe`` Jinja
  embedding (the only dynamic content is the float-derived ``y``/
  ``height`` numbers we compute ourselves; no user-controlled string
  lands in the markup).
"""

from __future__ import annotations

import wave
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import ModuleType

    Decoder = Callable[[Path], list[int] | None]

# ``aifc`` was removed from the stdlib in Python 3.13 (PEP 594). The
# AIFF decoder path is best-effort — when ``aifc`` is unavailable we
# fall through to the optional ``soundfile`` dependency. Typed as
# ``ModuleType | None`` so mypy --strict tracks the absent branch.
_aifc: ModuleType | None
try:
    import aifc as _aifc_imported
except ModuleNotFoundError:  # pragma: no cover — interpreter-version dependent
    _aifc = None
else:
    _aifc = _aifc_imported

log = get_logger("persona.audio_waveform")


# ---------------------------------------------------------------------------
# Return-type contracts
# ---------------------------------------------------------------------------


class WaveformResult(TypedDict, total=False):
    """Shape of :func:`generate_waveform`'s return value.

    The ``status`` discriminator tells the worker / route caller which
    branch fired. ``ok`` rows carry the rendered SVG metadata; the
    error branches carry only ``status`` + ``segment_id``.
    """

    status: Literal["ok", "missing", "error"]
    segment_id: int
    bars: int
    height: int
    svg_length: int
    reason: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_BAR_WIDTH_PX: Final[int] = 3
_BAR_GAP_PX: Final[int] = 1
_DEFAULT_BARS: Final[int] = 60
_DEFAULT_HEIGHT: Final[int] = 24

# Hard ceilings — refuse to render ridiculous shapes so a tampered
# caller cannot pin the worker on a 100000-bar request.
_MAX_BARS: Final[int] = 1024
_MAX_HEIGHT: Final[int] = 512


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _load_segment_path(segment_id: int) -> str | None:
    """Return the stored ``path`` for ``segment_id`` or ``None`` if absent.

    Parametrised SQL — the only user-controlled value (``segment_id``)
    is bound, never interpolated. ``None`` covers both "row missing"
    and "row carries an empty path" (retention sweep nulled it).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT path FROM audio_segment WHERE id = ?",
            (int(segment_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    raw_path = row["path"]
    stored = "" if raw_path is None else str(raw_path).strip()
    return stored or None


def _resolve_under_data_dir(stored_path: str) -> Path | None:
    """Resolve ``stored_path`` under ``data_dir`` with a containment guard.

    Mirrors :func:`app.web.routes.audio_player._resolve_path` — the DB
    always writes paths under ``data_dir`` but a tampered row must not
    coax this code path into reading arbitrary files. Returns ``None``
    when the resolution falls outside the data root.
    """
    settings = get_settings()
    candidate = (settings.data_dir / stored_path).resolve()
    data_root = settings.data_dir.resolve()
    try:
        candidate.relative_to(data_root)
    except ValueError:
        log.warning(
            "audio_waveform.path_escape_blocked",
            stored_path=stored_path,
            resolved=str(candidate),
        )
        return None
    return candidate


async def _persist_svg(segment_id: int, svg: str) -> None:
    """Write the rendered SVG + generation timestamp back to the row.

    Parametrised SQL — values are bound, the ``segment_id`` is coerced
    via ``int()`` first. The ``UPDATE`` is unconditional within the
    worker pipeline because :func:`generate_waveform` is only called
    when the column is still NULL; the route's regenerate endpoint
    deliberately re-overwrites on demand.
    """
    generated_at = datetime.now(tz=UTC).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE audio_segment
               SET waveform_svg = ?,
                   waveform_generated_at = ?
             WHERE id = ?
            """,
            (svg, generated_at, int(segment_id)),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Decoder pipeline (stdlib first, soundfile as last resort)
# ---------------------------------------------------------------------------


def _decode_pcm_frames(
    raw: bytes, sample_width: int, channels: int
) -> list[int] | None:
    """Convert raw little-endian PCM bytes into a list of per-frame peaks.

    Returns absolute amplitude per frame, averaged across channels when
    the source is multi-channel — matches the upstream Whisper
    preprocessor. ``None`` on degenerate input (frame size 0, empty
    buffer, unsupported sample width).
    """
    if sample_width not in (1, 2, 3, 4) or channels <= 0:
        return None
    frame_size = sample_width * channels
    if frame_size <= 0:
        return None
    total_frames = len(raw) // frame_size
    if total_frames <= 0:
        return None
    # 8-bit PCM is unsigned (offset by 128); wider widths are signed
    # little-endian per the WAV / AIFF (LSB-first) conventions.
    is_unsigned_8 = sample_width == 1
    peaks: list[int] = []
    for frame_index in range(total_frames):
        base = frame_index * frame_size
        acc = 0
        for channel_index in range(channels):
            offset = base + channel_index * sample_width
            sample_bytes = raw[offset : offset + sample_width]
            if is_unsigned_8:
                value = sample_bytes[0] - 128
            else:
                value = int.from_bytes(sample_bytes, "little", signed=True)
            if value < 0:
                value = -value
            acc += value
        peaks.append(acc // channels)
    return peaks


def _decode_with_wave(audio_path: Path) -> list[int] | None:
    """Decode a WAV / WAVE file via the stdlib ``wave`` module."""
    try:
        with wave.open(str(audio_path), "rb") as handle:
            channels = int(handle.getnchannels())
            sample_width = int(handle.getsampwidth())
            frame_count = int(handle.getnframes())
            raw = handle.readframes(frame_count)
    except (wave.Error, OSError, EOFError) as exc:
        log.info(
            "audio_waveform.wave_decode_failed",
            path=str(audio_path),
            error=str(exc),
        )
        return None
    return _decode_pcm_frames(raw, sample_width, channels)


def _decode_with_aifc(audio_path: Path) -> list[int] | None:
    """Decode an AIFF / AIFC file via the stdlib ``aifc`` module.

    AIFF is big-endian; we flip endianness per sample before reusing
    the little-endian PCM decoder so the peak math stays in one place.
    Returns ``None`` when the ``aifc`` module was dropped from this
    interpreter (Python 3.13+ per PEP 594) — the caller falls through
    to the ``soundfile`` path.
    """
    if _aifc is None:
        return None
    try:
        with _aifc.open(str(audio_path), "rb") as handle:
            channels = int(handle.getnchannels())
            sample_width = int(handle.getsampwidth())
            frame_count = int(handle.getnframes())
            raw = handle.readframes(frame_count)
    except (_aifc.Error, OSError, EOFError) as exc:
        log.info(
            "audio_waveform.aifc_decode_failed",
            path=str(audio_path),
            error=str(exc),
        )
        return None
    if sample_width <= 1 or not raw:
        # 8-bit AIFF is unsigned (same as WAV) — skip the swap.
        return _decode_pcm_frames(raw, sample_width, channels)
    # Big-endian → little-endian sample swap via bytearray slice flip.
    swapped = bytearray(len(raw))
    for index in range(0, len(raw), sample_width):
        chunk = raw[index : index + sample_width]
        swapped[index : index + sample_width] = chunk[::-1]
    return _decode_pcm_frames(bytes(swapped), sample_width, channels)


def _decode_with_soundfile(audio_path: Path) -> list[int] | None:
    """Last-resort decode via the optional ``soundfile`` dependency.

    Returns the per-frame absolute amplitude scaled into a 16-bit
    signed integer range so downstream peak bucketing produces the
    same numeric shape as the stdlib path.
    """
    try:
        import soundfile as sf  # noqa: PLC0415 — optional dep, lazy import
    except ImportError:
        return None
    try:
        data, _samplerate = sf.read(str(audio_path), always_2d=True)
    except Exception as exc:
        log.info(
            "audio_waveform.soundfile_decode_failed",
            path=str(audio_path),
            error=str(exc),
        )
        return None
    rows = data.shape[0]
    if rows == 0:
        return None
    peaks: list[int] = []
    int_max = 32767
    for row in data:
        # ``row`` is a numpy array of float / int per channel; mean of
        # absolute values keeps multi-channel input on the same scale
        # as the stdlib path which sums-then-divides.
        total = 0.0
        for sample in row:
            value = float(sample)
            if value < 0.0:
                value = -value
            total += value
        mean = total / float(len(row)) if len(row) else 0.0
        scaled = int(min(int_max, max(0, round(mean * int_max))))
        peaks.append(scaled)
    return peaks


_DECODERS: Final[tuple[tuple[tuple[str, ...], Decoder], ...]] = (
    ((".wav", ".wave"), _decode_with_wave),
    ((".aif", ".aiff", ".aifc"), _decode_with_aifc),
)


def _decode_audio(audio_path: Path) -> list[int] | None:
    """Pick the right decoder by extension; fall back to ``soundfile``.

    Returns the per-frame absolute amplitudes or ``None`` on any
    failure. The fallback ordering keeps the stdlib path on the
    happy path (zero optional deps) and only reaches for
    ``soundfile`` when the extension hints at an Opus / FLAC / etc.
    payload.
    """
    suffix = audio_path.suffix.lower()
    for extensions, decoder in _DECODERS:
        if suffix in extensions:
            result = decoder(audio_path)
            if result is not None:
                return result
            # Fall through to the soundfile path — covers a WAV file
            # whose header lies about its width, for instance.
            break
    return _decode_with_soundfile(audio_path)


# ---------------------------------------------------------------------------
# Peak bucketing + SVG rendering
# ---------------------------------------------------------------------------


def _bucket_peaks(samples: list[int], bars: int) -> list[float]:
    """Downsample ``samples`` into ``bars`` per-bucket peak amplitudes.

    The last bucket absorbs any remainder so no frames are dropped.
    Output is normalised into ``[0.0, 1.0]`` by the global maximum;
    an all-silence buffer returns zeros so the SVG renderer draws a
    flat baseline rather than dividing by zero.
    """
    total = len(samples)
    if total == 0 or bars <= 0:
        return [0.0] * max(bars, 0)
    actual_bars = min(bars, total)
    step = total // actual_bars
    raw_peaks: list[int] = []
    raw_max = 0
    for index in range(actual_bars):
        start = index * step
        end = total if index == actual_bars - 1 else start + step
        bucket_max = 0
        for sample in samples[start:end]:
            bucket_max = max(bucket_max, sample)
        raw_peaks.append(bucket_max)
        raw_max = max(raw_max, bucket_max)
    # Pad to the requested bar count so the SVG width stays predictable
    # even when the source has fewer frames than bars.
    while len(raw_peaks) < bars:
        raw_peaks.append(0)
    if raw_max <= 0:
        return [0.0] * bars
    normalised: list[float] = []
    for value in raw_peaks:
        ratio = value / raw_max
        # Defensive clamp — float math could nudge over 1.0 by an
        # epsilon, which would break the ``height`` attribute math.
        if ratio < 0.0:
            ratio = 0.0
        elif ratio > 1.0:
            ratio = 1.0
        normalised.append(ratio)
    return normalised


def _render_svg(peaks: list[float], bars: int, height: int) -> str:
    """Render a static ``<svg>`` document for ``peaks``.

    Width is ``bars * (bar_width + gap)``. Each bucket is one
    ``<rect>`` anchored at the vertical centre — silent buckets
    render as a 1-px baseline so the eye distinguishes "no audio"
    from "missing thumbnail". ``currentColor`` lets the host template
    pick the fill via CSS.
    """
    stride = _BAR_WIDTH_PX + _BAR_GAP_PX
    width = bars * stride
    centre = height / 2.0
    min_bar_height_px = 1.0
    rects: list[str] = []
    for index in range(bars):
        amplitude = peaks[index] if index < len(peaks) else 0.0
        bar_height = max(amplitude * height, min_bar_height_px)
        x_pos = index * stride
        y_pos = centre - bar_height / 2.0
        rects.append(
            f'<rect x="{x_pos}" y="{y_pos:.2f}" '
            f'width="{_BAR_WIDTH_PX}" height="{bar_height:.2f}" '
            f'rx="0.5" fill="currentColor"/>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" '
        f'preserveAspectRatio="none" '
        f'role="img" aria-label="audio waveform">'
        f"{''.join(rects)}"
        f"</svg>"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def generate_waveform(
    segment_id: int,
    bars: int = _DEFAULT_BARS,
    height: int = _DEFAULT_HEIGHT,
) -> WaveformResult:
    """Render and persist an SVG waveform thumbnail for ``segment_id``.

    Args:
        segment_id: Primary key of the ``audio_segment`` row.
        bars: Number of vertical bars in the rendered SVG. Clamped
            into ``[1, _MAX_BARS]`` so a tampered caller cannot pin
            the worker on an absurd request.
        height: Pixel height of the SVG canvas. Clamped into
            ``[1, _MAX_HEIGHT]`` for the same reason.

    Returns:
        A :class:`WaveformResult` dict:

          * ``{"status": "missing", "segment_id": ...}`` — row absent,
            path NULL, file removed from disk, or path escaped
            ``data_dir`` containment check.
          * ``{"status": "error", "segment_id": ..., "reason": ...}``
            — file exists but no decoder could read it (corrupt
            header, unsupported codec, ``soundfile`` not installed
            for a non-stdlib format).
          * ``{"status": "ok", "segment_id": ..., "bars": ...,
            "height": ..., "svg_length": ...}`` — happy path. The
            SVG is committed to the row before returning.

    The function is idempotent — calling it again on the same row
    re-renders + overwrites ``waveform_svg`` / ``waveform_generated_at``.
    The route's regenerate endpoint relies on this; the worker only
    calls it on still-NULL rows.
    """
    bars = max(1, min(int(bars), _MAX_BARS))
    height = max(1, min(int(height), _MAX_HEIGHT))

    stored_path = await _load_segment_path(segment_id)
    if stored_path is None:
        log.info("audio_waveform.missing_row", segment_id=segment_id)
        return {"status": "missing", "segment_id": int(segment_id)}

    resolved = _resolve_under_data_dir(stored_path)
    if resolved is None or not resolved.exists():
        log.info(
            "audio_waveform.missing_file",
            segment_id=segment_id,
            stored_path=stored_path,
        )
        return {"status": "missing", "segment_id": int(segment_id)}

    samples = _decode_audio(resolved)
    if samples is None or not samples:
        log.warning(
            "audio_waveform.decode_failed",
            segment_id=segment_id,
            stored_path=stored_path,
            suffix=resolved.suffix.lower(),
        )
        return {
            "status": "error",
            "segment_id": int(segment_id),
            "reason": "decode_failed",
        }

    peaks = _bucket_peaks(samples, bars)
    svg = _render_svg(peaks, bars, height)
    await _persist_svg(segment_id, svg)
    log.info(
        "audio_waveform.ok",
        segment_id=segment_id,
        bars=bars,
        height=height,
        svg_length=len(svg),
        frames=len(samples),
    )
    return {
        "status": "ok",
        "segment_id": int(segment_id),
        "bars": bars,
        "height": height,
        "svg_length": len(svg),
    }


__all__ = ["WaveformResult", "generate_waveform"]
