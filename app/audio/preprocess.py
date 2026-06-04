"""Audio cleanup pipeline — band-pass filter + loudness normalisation.

Two passes:
    1. **Band-pass 80 Hz - 4 kHz** via Butterworth IIR (``scipy.signal``).
       Removes HVAC rumble below 80 Hz and dental-S sibilance / hiss
       above 4 kHz, leaving the speech-intelligibility band intact. This
       roughly halves the encoded bitrate the Opus / Encodec stages
       need to spend at the same quality.
    2. **Loudness normalisation to -16 LUFS** (the EBU R128 / streaming
       reference level used by Spotify et al.) via :mod:`pyloudnorm` if
       available, falling back to a deterministic peak-normalisation to
       -3 dBFS so the output is still at a consistent level on machines
       without the optional dep.

The function deliberately *never* raises — failures degrade gracefully
to a less-processed but still-usable array, with a single log line per
event. The worker depends on always getting an array back, so a missing
``scipy`` install simply skips the filter and a missing ``pyloudnorm``
falls back to peak normalisation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover — types only
    import numpy as np

log = get_logger("persona.audio.preprocess")

HIGHPASS_HZ: float = 80.0
"""Removes mains hum, AC rumble, table thumps."""

LOWPASS_HZ: float = 4_000.0
"""Speech intelligibility tops out near 4 kHz; cutting higher saves bits."""

TARGET_LUFS: float = -16.0
"""Streaming reference loudness. Conservative — keeps ASR happy."""

PEAK_FALLBACK_DBFS: float = -3.0
"""Used when pyloudnorm is missing; ``-3 dBFS`` leaves comfortable headroom."""


try:
    import numpy as np

    _NUMPY_OK = True
except ImportError as exc:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _NUMPY_OK = False
    log.warning("audio.preprocess.numpy_missing", error=str(exc))

try:
    from scipy.signal import (  # type: ignore[import-untyped, import-not-found, unused-ignore]
        butter,
        sosfiltfilt,
    )

    _SCIPY_OK = True
except ImportError as exc:  # pragma: no cover
    butter = None  # type: ignore[assignment, unused-ignore]
    sosfiltfilt = None  # type: ignore[assignment, unused-ignore]
    _SCIPY_OK = False
    log.warning("audio.preprocess.scipy_missing", error=str(exc))

try:
    import pyloudnorm as pyln  # type: ignore[import-untyped, import-not-found, unused-ignore]

    _PYLOUDNORM_OK = True
except ImportError:  # pragma: no cover — purely optional
    pyln = None  # type: ignore[assignment, unused-ignore]
    _PYLOUDNORM_OK = False


def preprocess(audio_arr: np.ndarray, sample_rate: int) -> np.ndarray:
    """Return a cleaned + loudness-matched copy of ``audio_arr``.

    The input is treated as mono float32. The output is the same shape
    and dtype as the input. Designed for short (<= 60 s) chunks so the
    whole array stays in RAM and no streaming filter state has to be
    threaded between calls.
    """
    if not _NUMPY_OK:
        log.warning("audio.preprocess.skipped_numpy_missing")
        return audio_arr

    assert np is not None
    out = np.asarray(audio_arr, dtype=np.float32)

    if _SCIPY_OK:
        out = _bandpass(out, sample_rate=sample_rate)
    else:
        log.debug("audio.preprocess.bandpass_skipped_scipy_missing")

    out = (
        _normalise_lufs(out, sample_rate=sample_rate)
        if _PYLOUDNORM_OK
        else _peak_normalise(out)
    )

    return out


def _bandpass(audio: np.ndarray, *, sample_rate: int) -> np.ndarray:
    """Apply an 80 Hz - 4 kHz 4th-order Butterworth band-pass.

    Uses :func:`scipy.signal.sosfiltfilt` (second-order sections, zero
    phase) so the output has no phase distortion — important for VAD
    + transcription downstream. Catches *all* exceptions and returns
    the original array unmodified rather than poisoning the pipeline.
    """
    assert butter is not None and sosfiltfilt is not None and np is not None
    nyquist = sample_rate * 0.5
    low = HIGHPASS_HZ / nyquist
    high = min(LOWPASS_HZ, nyquist - 1.0) / nyquist
    if not 0.0 < low < high < 1.0:
        # Sample-rate too low for the requested band — skip the filter.
        log.debug(
            "audio.preprocess.bandpass_invalid_band",
            sample_rate=sample_rate,
            low=low,
            high=high,
        )
        return audio
    try:
        sos = butter(N=4, Wn=[low, high], btype="bandpass", output="sos")
        filtered = sosfiltfilt(sos, audio)
        return np.asarray(filtered, dtype=np.float32)
    except Exception as exc:
        log.warning("audio.preprocess.bandpass_failed", error=str(exc))
        return audio


def _normalise_lufs(audio: np.ndarray, *, sample_rate: int) -> np.ndarray:
    """Loudness-normalise ``audio`` to :data:`TARGET_LUFS` via pyloudnorm."""
    assert pyln is not None and np is not None
    try:
        meter: Any = pyln.Meter(sample_rate)
        loudness = meter.integrated_loudness(audio)
        # pyloudnorm returns ``-inf`` for silent input — fall back to peak.
        if not np.isfinite(loudness):
            return _peak_normalise(audio)
        normalised = pyln.normalize.loudness(audio, loudness, TARGET_LUFS)
        # The result may briefly exceed [-1, 1] after gain; clip to avoid
        # downstream clipping warnings from the encoder stage.
        return np.clip(np.asarray(normalised, dtype=np.float32), -1.0, 1.0)
    except Exception as exc:
        log.warning("audio.preprocess.lufs_failed", error=str(exc))
        return _peak_normalise(audio)


def _peak_normalise(audio: np.ndarray) -> np.ndarray:
    """Scale ``audio`` so its peak sits at :data:`PEAK_FALLBACK_DBFS`."""
    assert np is not None
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= 0.0:
        return audio
    target_amp = float(10.0 ** (PEAK_FALLBACK_DBFS / 20.0))
    gain = target_amp / peak
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32, copy=False)


__all__ = [
    "HIGHPASS_HZ",
    "LOWPASS_HZ",
    "PEAK_FALLBACK_DBFS",
    "TARGET_LUFS",
    "preprocess",
]
