"""Speech-only audio capture pipeline — v1.11 feature 1/3.

Submodules:
    * :mod:`app.audio.capture` — sounddevice ring buffer, 16 kHz mono.
    * :mod:`app.audio.vad` — silero-vad speech segment detector.
    * :mod:`app.audio.preprocess` — band-pass + EBU R128 normalisation.
    * :mod:`app.audio.encode` — Encodec / Opus / ffmpeg encoder cascade.
    * :mod:`app.audio.transcribe` — Whisper STT (feature 2/3).

Every dependency is imported defensively (``try`` / ``except ImportError``)
so the package is importable on a vanilla Python install. Functions that
need a missing extra return a sentinel ``"missing_dep"`` status rather
than raising, so the worker can keep running and log a single warning
instead of crashing the whole capture loop.
"""

from app.audio.transcribe import transcribe_segment

__all__ = ["transcribe_segment"]
