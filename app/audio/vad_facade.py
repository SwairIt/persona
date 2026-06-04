"""VAD backend selector (v1.13).

The audio worker imports :func:`detect_speech_segments` from here
instead of from a specific backend module. The setting
``audio_vad_backend`` picks the implementation:

- ``"webrtcvad"`` (default) — pure C, ~4 KB, no torch.
- ``"silero"`` — original torch-backed backend, kept for users on
  machines that already have torch installed.

Missing backends fall through to the next option. If both are
unavailable the worker gets ``{"status": "missing_dep", ...}`` and
treats the chunk as silence (the original silero contract).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.settings import get_settings

if TYPE_CHECKING:
    import numpy as np

log = get_logger("persona.audio.vad.facade")


async def detect_speech_segments(
    audio_arr: np.ndarray,
    sample_rate: int,
) -> list[tuple[float, float]] | dict[str, Any]:
    """Dispatch to the configured VAD backend."""
    backend = get_settings().audio_vad_backend.lower()

    if backend == "webrtcvad":
        from app.audio import vad_webrtc  # noqa: PLC0415

        result = await vad_webrtc.detect_speech_segments(audio_arr, sample_rate)
        if isinstance(result, dict) and result.get("status") == "missing_dep":
            log.info("audio.vad.fallback_to_silero", reason=result.get("missing"))
            from app.audio import vad as vad_silero  # noqa: PLC0415

            return await vad_silero.detect_speech_segments(audio_arr, sample_rate)
        return result

    # silero or anything unknown — default path
    from app.audio import vad as vad_silero  # noqa: PLC0415

    return await vad_silero.detect_speech_segments(audio_arr, sample_rate)


__all__ = ["detect_speech_segments"]
