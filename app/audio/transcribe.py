"""Whisper-based speech-to-text for v1.11 audio segments.

Acts as the "lossless content" track that survives the audio-file
retention purge: the raw ``.wav`` / ``.opus`` segment is the *signal*,
but the transcript is the *content* — and content is what we keep
forever, even after the audio bytes are deleted from disk.

Two backends are tried in order:

1. :mod:`whisper` (OpenAI's reference implementation,
   ``openai-whisper`` on PyPI).
2. :mod:`faster_whisper` (CTranslate2 port, ~4x faster on CPU).

Both are optional dependencies — when neither is importable the
function returns ``None`` and logs a single warning at first use, so
the surrounding audio pipeline continues to write segments and the
``transcript`` column simply stays ``NULL`` until the user installs a
backend.

The chosen model is :data:`settings.audio_whisper_model` (default
``"small"``, ~244 MB, a reasonable accuracy/RAM trade-off for desktop
hardware). The first transcription pays the model-load cost; the
loaded model is cached at module scope and reused for every
subsequent call.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger("persona.audio.transcribe")


_BACKEND_OPENAI = "openai-whisper"
_BACKEND_FASTER = "faster-whisper"
_BACKEND_NONE = "none"

_backend: str | None = None
"""Resolved backend name; ``None`` until first probe, ``"none"`` if neither lib is installed."""

_model: Any = None
"""Loaded Whisper model (backend-specific type). Lazy, process-lifetime."""

_model_lock = threading.Lock()
"""Guards backend probe + model load — multiple worker calls must not race the load."""

_warned_missing = False
"""One-shot flag so we log the "no backend installed" warning exactly once."""


async def transcribe_segment(
    audio_path: Path | str,
    locale_hint: str | None = None,
) -> str | None:
    """Transcribe a single audio segment with Whisper.

    Args:
        audio_path: Filesystem path to a finished audio file written
            by ``audio_worker``. Must exist; non-existent paths return
            ``None`` (and log a warning).
        locale_hint: Optional BCP-47-ish language code (e.g. ``"en"``,
            ``"ru"``). When provided, both backends use it directly
            instead of running their own language-detection pass —
            faster and more accurate for known-locale recordings.

    Returns:
        The transcribed text (stripped, possibly multi-line), or
        ``None`` when:
          * neither Whisper backend is installed,
          * the audio file does not exist,
          * the backend raised during inference.

        Callers store the result in ``audio_segment.transcript`` and
        rely on the column staying ``NULL`` when this returns ``None``.
    """
    path = Path(audio_path)
    if not path.exists():
        log.warning("audio.transcribe.missing_file", path=str(path))
        return None

    backend = await asyncio.to_thread(_resolve_backend)
    if backend == _BACKEND_NONE:
        return None

    try:
        text = await asyncio.to_thread(_transcribe_sync, path, backend, locale_hint)
    except Exception as exc:
        # Backend-specific failure (OOM, corrupt WAV, missing ffmpeg, …).
        # Best-effort side-channel: caller still commits the segment row
        # with ``transcript = NULL`` so the audio file is not orphaned.
        log.warning(
            "audio.transcribe.failed",
            path=str(path),
            backend=backend,
            error=str(exc),
        )
        return None

    if text is None:
        return None

    cleaned = text.strip()
    if not cleaned:
        # An empty transcript ("just background noise") is meaningful —
        # we still return ``""`` so the caller can distinguish
        # "transcribed but silent" from "backend missing / failed".
        log.info("audio.transcribe.empty", path=str(path), backend=backend)
        return ""

    log.info(
        "audio.transcribe.ok",
        path=str(path),
        backend=backend,
        chars=len(cleaned),
        locale_hint=locale_hint,
    )
    return cleaned


def _resolve_backend() -> str:
    """Probe + cache which Whisper backend (if any) is installed.

    Runs under :data:`_model_lock` so concurrent worker calls cannot
    each pay the import + model-load cost. The first caller wins; every
    subsequent call short-circuits on the module-level :data:`_backend`.

    Returns one of :data:`_BACKEND_OPENAI`, :data:`_BACKEND_FASTER`,
    :data:`_BACKEND_NONE`.
    """
    global _backend, _model, _warned_missing  # noqa: PLW0603 — module-level cache
    cached = _backend
    if cached is not None:
        return cached

    with _model_lock:
        # Double-checked locking: another thread may have populated the
        # cache between the outer check and our acquiring the lock.
        cached = _backend
        if cached is not None:
            return cached

        settings = get_settings()
        model_name = settings.audio_whisper_model

        # Preference order: openai-whisper first (richer model selection
        # + de-facto reference behaviour), faster-whisper as the
        # CPU-friendly fallback. Both are pure-Python imports at the
        # top, no native handshake — so the try/except is cheap.
        try:
            import whisper  # noqa: PLC0415 — optional dep, lazy import

            try:
                _model = whisper.load_model(model_name)
            except Exception as exc:
                log.warning(
                    "audio.transcribe.openai_whisper_load_failed",
                    model=model_name,
                    error=str(exc),
                )
            else:
                _backend = _BACKEND_OPENAI
                log.info(
                    "audio.transcribe.backend_ready",
                    backend=_backend,
                    model=model_name,
                )
                return _backend
        except ImportError:
            pass

        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            try:
                _model = WhisperModel(model_name, device="cpu", compute_type="int8")
            except Exception as exc:
                log.warning(
                    "audio.transcribe.faster_whisper_load_failed",
                    model=model_name,
                    error=str(exc),
                )
            else:
                _backend = _BACKEND_FASTER
                log.info(
                    "audio.transcribe.backend_ready",
                    backend=_backend,
                    model=model_name,
                )
                return _backend
        except ImportError:
            pass

        _backend = _BACKEND_NONE
        if not _warned_missing:
            _warned_missing = True
            log.warning(
                "audio.transcribe.no_backend",
                hint=(
                    "Install 'openai-whisper' or 'faster-whisper' to enable "
                    "transcription; audio segments will still capture but the "
                    "transcript column will stay NULL."
                ),
            )
        return _backend


def _transcribe_sync(
    path: Path,
    backend: str,
    locale_hint: str | None,
) -> str | None:
    """Run inference under whichever backend was resolved. Sync — CPU bound.

    Always invoked from inside ``asyncio.to_thread`` so the event loop
    stays responsive. Returns the raw transcript text (caller strips
    + normalises empty results).
    """
    if _model is None:
        return None

    if backend == _BACKEND_OPENAI:
        # openai-whisper returns a dict; ``text`` is the concatenated
        # transcript. ``language=None`` triggers its internal detection.
        result = _model.transcribe(
            str(path),
            language=locale_hint,
            fp16=False,
        )
        text = result.get("text") if isinstance(result, dict) else None
        return str(text) if text is not None else None

    if backend == _BACKEND_FASTER:
        # faster-whisper returns (segments_iter, info). Each segment has
        # a ``.text`` attribute. We concatenate with spaces; callers can
        # always re-segment from the raw transcript if needed.
        segments, _info = _model.transcribe(
            str(path),
            language=locale_hint,
            beam_size=1,
        )
        parts: list[str] = []
        for seg in segments:
            piece = getattr(seg, "text", None)
            if piece:
                parts.append(str(piece))
        return " ".join(parts) if parts else ""

    return None
