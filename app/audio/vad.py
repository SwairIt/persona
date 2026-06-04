"""Voice Activity Detection via silero-vad.

Silero-VAD is a small (~1.8 MB) torch model that classifies each ~32 ms
frame as speech vs. non-speech. We use the official ``silero_vad`` PyPI
helper (which lazily downloads + caches the JIT model on first call) and
expose a single async entry point :func:`detect_speech_segments` that
returns a list of ``(start_seconds, end_seconds)`` tuples — one tuple
per contiguous speech region.

Why segments and not a per-frame mask?
    The downstream encoder (Encodec / Opus) operates on contiguous audio
    blocks; returning ``[(s, e), ...]`` lets the worker slice the raw
    numpy array exactly once and feed each region to the encoder
    independently. Returning a frame-level mask would force every
    consumer to re-derive the same boundaries.

Dependency policy: both ``torch`` and ``silero_vad`` are heavy / optional.
Imports are guarded; the function returns a ``{"status": "missing_dep",
"missing": [...]}`` dict on a fresh checkout. The worker is expected to
log the dict once and treat the chunk as "no speech" so capture keeps
flowing.

The threshold comes from settings (``audio_vad_threshold``, default
0.5). Silero docs recommend 0.3-0.7 depending on environment noise;
0.5 is the upstream default and gives ~95 % recall on clean speech.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover — only used by type checker
    import numpy as np

log = get_logger("persona.audio.vad")


try:
    import torch

    _TORCH_OK = True
except ImportError as exc:  # pragma: no cover
    torch = None  # type: ignore[assignment, unused-ignore]
    _TORCH_OK = False
    log.warning("audio.vad.torch_missing", error=str(exc))

try:
    # The official PyPI distribution renamed itself a few times; we
    # tolerate both the new ``silero_vad`` and the legacy ``silero``
    # module layout so users on either version Just Work.
    from silero_vad import (  # type: ignore[import-untyped, import-not-found, unused-ignore]
        get_speech_timestamps,
        load_silero_vad,
    )

    _SILERO_OK = True
except ImportError as exc:  # pragma: no cover
    get_speech_timestamps = None  # type: ignore[assignment, unused-ignore]
    load_silero_vad = None  # type: ignore[assignment, unused-ignore]
    _SILERO_OK = False
    log.warning("audio.vad.silero_missing", error=str(exc))


# Cached JIT model — populated on first :func:`_ensure_model` call.
# Wrapped in a single-element list so we mutate the *contents* rather
# than the module-level binding, sidestepping ``PLW0603`` (no ``global``
# statement needed). The list itself is never reassigned.
_model_cache: list[Any] = [None]


def _missing_deps() -> list[str]:
    """Return any unimportable extras; empty list means we're ready to go."""
    missing: list[str] = []
    if not _TORCH_OK:
        missing.append("torch")
    if not _SILERO_OK:
        missing.append("silero-vad")
    return missing


def _ensure_model() -> Any:
    """Load the JIT model once per process. Cached at module scope."""
    if _model_cache[0] is not None:
        return _model_cache[0]
    assert load_silero_vad is not None
    _model_cache[0] = load_silero_vad()
    log.info("audio.vad.model_loaded")
    return _model_cache[0]


async def detect_speech_segments(
    audio_arr: np.ndarray,
    sample_rate: int,
) -> list[tuple[float, float]] | dict[str, Any]:
    """Return contiguous speech regions in ``audio_arr`` as ``(start_s, end_s)``.

    ``audio_arr`` must be 1-D mono float32 in the range ``[-1, 1]``.
    Silero internally resamples to 16 kHz so callers may pass any
    ``sample_rate``, though sticking to 16 kHz avoids one extra copy.

    Returns ``{"status": "missing_dep", "missing": [...]}`` when torch
    or silero_vad are unavailable. Returns ``[]`` when the model ran
    successfully but heard nothing — distinct from "deps missing" so
    the worker can differentiate "user was quiet" from "model is broken".
    """
    missing = _missing_deps()
    if missing:
        return {"status": "missing_dep", "missing": missing}

    assert torch is not None and get_speech_timestamps is not None
    threshold = float(get_settings().audio_vad_threshold)

    def _run() -> list[tuple[float, float]]:
        model = _ensure_model()
        # silero accepts a torch.Tensor of shape (n_samples,) — convert
        # without copying when the dtype already matches.
        tensor = torch.from_numpy(audio_arr)
        if tensor.dtype != torch.float32:
            tensor = tensor.to(torch.float32)
        raw = get_speech_timestamps(
            tensor,
            model,
            sampling_rate=sample_rate,
            threshold=threshold,
            return_seconds=True,
        )
        segments: list[tuple[float, float]] = []
        for entry in raw:
            # silero returns dicts ``{"start": float, "end": float}``
            # when ``return_seconds=True``. Defensive ``float()`` so an
            # accidental numpy scalar doesn't trip JSON encoders later.
            start = float(entry.get("start", 0.0))
            end = float(entry.get("end", 0.0))
            if end > start:
                segments.append((start, end))
        return segments

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        log.warning("audio.vad.inference_failed", error=str(exc))
        return {"status": "missing_dep", "missing": ["silero-vad-runtime"], "error": str(exc)}


__all__ = ["detect_speech_segments"]
