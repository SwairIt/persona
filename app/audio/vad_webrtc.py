"""WebRTC VAD backend (v1.13).

Pure C implementation, ~4 KB on disk, no torch / scipy / numpy big-deps.
Wraps ``webrtcvad`` with a contiguous-region detector that returns the
same ``[(start_seconds, end_seconds), ...]`` shape as the silero
backend at :mod:`app.audio.vad`. The audio worker can swap between the
two via the ``audio_vad_backend`` setting.

WebRTC VAD requires:
- mono int16 PCM
- 8000 / 16000 / 32000 / 48000 Hz sample rate
- 10 / 20 / 30 ms frame size

Our capture pipeline produces float32 at 8 or 16 kHz; we convert in
one numpy multiply + cast. WebRTC's aggressiveness is 0-3; we map
``audio_vad_threshold`` (0..1) onto that range with the more familiar
"higher number = stricter" semantics.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.settings import get_settings

if TYPE_CHECKING:
    import numpy as np

log = get_logger("persona.audio.vad.webrtc")

try:
    import webrtcvad

    _WEBRTC_OK = True
except ImportError as exc:  # pragma: no cover
    webrtcvad = None  # type: ignore[assignment]
    _WEBRTC_OK = False
    log.info("audio.vad.webrtc_missing", error=str(exc))


_FRAME_MS = 30
_MIN_SEGMENT_S = 0.25  # drop micro-coughs / clicks


def _threshold_to_aggressiveness(threshold: float) -> int:
    """Map silero-style 0..1 confidence onto WebRTC 0..3 aggressiveness."""
    if threshold < 0.3:
        return 0
    if threshold < 0.5:
        return 1
    if threshold < 0.7:
        return 2
    return 3


def _missing_deps() -> list[str]:
    if not _WEBRTC_OK:
        return ["webrtcvad"]
    return []


async def detect_speech_segments(
    audio_arr: np.ndarray,
    sample_rate: int,
) -> list[tuple[float, float]] | dict[str, Any]:
    """Return contiguous voiced regions, same shape as silero backend.

    Returns ``{"status": "missing_dep", "missing": [...]}`` when
    ``webrtcvad`` is not installed. Returns ``[]`` for a clean run with
    no speech detected.
    """
    missing = _missing_deps()
    if missing:
        return {"status": "missing_dep", "missing": missing}

    assert webrtcvad is not None
    if sample_rate not in (8000, 16000, 32000, 48000):
        return {
            "status": "missing_dep",
            "missing": [],
            "error": f"webrtcvad: unsupported sample rate {sample_rate}",
        }

    threshold = float(get_settings().audio_vad_threshold)
    aggressiveness = _threshold_to_aggressiveness(threshold)
    samples_per_frame = int(sample_rate * _FRAME_MS / 1000)

    def _run() -> list[tuple[float, float]]:
        import numpy as np_local  # noqa: PLC0415

        vad = webrtcvad.Vad(aggressiveness)
        clipped = np_local.clip(audio_arr, -1.0, 1.0)
        # float32 [-1, 1] → int16 PCM little-endian
        int16 = (clipped * 32767.0).astype(np_local.int16)
        total_frames = len(int16) // samples_per_frame

        segments: list[tuple[float, float]] = []
        current_start: float | None = None

        for i in range(total_frames):
            start_idx = i * samples_per_frame
            end_idx = start_idx + samples_per_frame
            frame_bytes = int16[start_idx:end_idx].tobytes()
            is_speech = vad.is_speech(frame_bytes, sample_rate)
            ts = i * (_FRAME_MS / 1000.0)

            if is_speech and current_start is None:
                current_start = ts
            elif not is_speech and current_start is not None:
                segment_end = ts
                if segment_end - current_start >= _MIN_SEGMENT_S:
                    segments.append((current_start, segment_end))
                current_start = None

        if current_start is not None:
            tail_end = total_frames * (_FRAME_MS / 1000.0)
            if tail_end - current_start >= _MIN_SEGMENT_S:
                segments.append((current_start, tail_end))

        return segments

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        log.warning("audio.vad.webrtc_failed", error=str(exc))
        return {
            "status": "missing_dep",
            "missing": ["webrtcvad-runtime"],
            "error": str(exc),
        }


__all__ = ["detect_speech_segments"]
