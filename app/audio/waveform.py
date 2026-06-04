"""Inline waveform peak extraction for ``audio_segment`` rows.

Companion to the inline player added in
:mod:`app.web.routes.audio_player` — that module calls
:func:`compute_waveform_peaks` and serialises the result into the JSON
endpoint backing the SVG waveform under each ``<audio controls>`` tag.

Design notes
------------

* ``soundfile`` + ``numpy`` are **optional** dependencies. Either
  missing → :func:`compute_waveform_peaks` returns ``[]`` and emits a
  *single* ``waveform.missing_dep`` info log the first time it happens.
  The player route degrades gracefully: no peaks → an empty SVG (the
  ``<audio>`` element still works).
* The peak vector is a simple per-bucket ``max(|sample|)`` downsample,
  not RMS — visually crisper bars for speech, and an order of magnitude
  cheaper than an FFT-backed loudness curve. Output is clamped to
  ``[0.0, 1.0]`` so the SVG renderer can scale it without renormalising.
* Multi-channel input is collapsed to mono via per-frame mean before
  downsampling; matches the upstream Whisper preprocessor and keeps
  the waveform single-track even for the (rare) stereo capture.
* Every failure path returns ``[]`` rather than raising. The route
  treats "no peaks" identically to "missing file" — the user sees the
  player without bars, which is the same correctness signal an
  exception in this layer would surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger("persona.audio.waveform")

# One-shot guard so the "no soundfile / numpy installed" message only
# appears the first time a route asks for peaks. Subsequent calls stay
# silent so the access log isn't flooded.
_warned_missing_dep = False


def _try_import_deps() -> tuple[Any, Any] | None:
    """Import ``soundfile`` + ``numpy`` lazily; return ``None`` if either is missing.

    Both libraries are heavy optional deps. We import inside the function
    (not at module load) so the rest of the audio module remains usable
    on a vanilla install. The one-shot warning lives here so callers
    cannot accidentally double-log by retrying.
    """
    global _warned_missing_dep  # noqa: PLW0603 — module-level one-shot guard
    try:
        import numpy as np  # noqa: PLC0415 — optional dep, lazy import
        import soundfile as sf  # noqa: PLC0415 — optional dep, lazy import
    except ImportError as exc:
        if not _warned_missing_dep:
            _warned_missing_dep = True
            log.info(
                "waveform.missing_dep",
                hint=(
                    "Install 'soundfile' and 'numpy' to render inline audio "
                    "waveforms; the player will still stream audio without bars."
                ),
                error=str(exc),
            )
        return None
    return sf, np


def _decode_mono(audio_path: Path, sf: Any, np: Any) -> Any | None:
    """Decode ``audio_path`` to a 1D float32 array of per-frame amplitude.

    Returns ``None`` on any failure (unreadable file, empty buffer).
    Multi-channel input is collapsed to mono via per-frame mean of the
    absolute sample values — matches the upstream Whisper preprocessor
    and keeps the waveform single-track even for stereo capture.
    """
    try:
        data, _samplerate = sf.read(str(audio_path), always_2d=True)
    except Exception as exc:
        log.warning(
            "waveform.read_failed",
            path=str(audio_path),
            error=str(exc),
        )
        return None
    if data.size == 0:
        log.info("waveform.empty_buffer", path=str(audio_path))
        return None
    mono = np.mean(np.abs(data), axis=1).astype("float32", copy=False)
    if int(mono.shape[0]) == 0:
        return None
    return mono


def _bucket_peaks(mono: Any, np: Any, buckets: int) -> list[float]:
    """Downsample ``mono`` into ``buckets`` per-bucket peak amplitudes.

    The last bucket absorbs any remainder so no frames are dropped.
    Output is renormalised into ``[0.0, 1.0]`` by dividing by the global
    max; an all-silence buffer (max == 0) returns zeros so the SVG
    renderer draws a flat baseline rather than dividing by zero.
    """
    total_frames = int(mono.shape[0])
    step = total_frames // buckets
    raw_peaks: list[float] = []
    raw_max = 0.0
    for index in range(buckets):
        start = index * step
        end = total_frames if index == buckets - 1 else start + step
        bucket = mono[start:end]
        value = float(np.max(bucket)) if bucket.size > 0 else 0.0
        raw_peaks.append(value)
        raw_max = max(raw_max, value)
    if raw_max <= 0.0:
        return [0.0] * buckets
    normalised: list[float] = []
    for value in raw_peaks:
        ratio = value / raw_max
        # Defensive clamp — float math could nudge over 1.0 by an
        # epsilon, which would break ``height="N%"`` downstream.
        normalised.append(min(1.0, max(0.0, ratio)))
    return normalised


def compute_waveform_peaks(audio_path: Path, samples: int = 200) -> list[float]:
    """Downsample ``audio_path`` to at most ``samples`` peak values in ``[0, 1]``.

    Args:
        audio_path: Filesystem path to an audio file (``.opus`` / ``.wav``
            in practice; ``soundfile`` decodes anything libsndfile knows
            how to read).
        samples: Target bucket count. The returned list has at most this
            many entries — when the source has fewer frames than buckets
            we just emit ``len(frames)`` peaks rather than zero-padding.

    Returns:
        A list of floats in ``[0.0, 1.0]`` representing per-bucket peak
        amplitude, ready to drive ``<rect height="...">`` bars in the
        SVG waveform. Returns ``[]`` on any of:

          * ``soundfile`` or ``numpy`` not installed;
          * file does not exist on disk;
          * file is unreadable (corrupt header, unsupported codec,
            permission denied);
          * caller passed ``samples <= 0``;
          * the decoded buffer is empty.

        The route module renders "no peaks" identically to "missing
        file" — bar-less player, audio still streams.
    """
    if samples <= 0 or not audio_path.exists():
        if samples > 0:
            log.info("waveform.missing_file", path=str(audio_path))
        return []
    deps = _try_import_deps()
    if deps is None:
        return []
    sf, np = deps
    mono = _decode_mono(audio_path, sf, np)
    if mono is None:
        return []
    # Bucket count is the minimum of requested samples and actual frames
    # so a 50-frame clip rendered into a 200-bucket request does not
    # produce 150 empty buckets. The SVG width still spans the available
    # peaks at full stretch.
    buckets = min(samples, int(mono.shape[0]))
    peaks = _bucket_peaks(mono, np, buckets)
    log.info(
        "waveform.computed",
        path=str(audio_path),
        samples=len(peaks),
        frames=int(mono.shape[0]),
    )
    return peaks


__all__ = ["compute_waveform_peaks"]
