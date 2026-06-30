"""Microphone capture — 16 kHz mono, 30-second rolling chunks.

The recorder runs sounddevice's :class:`InputStream` in a background
thread and feeds raw float32 frames into an :class:`numpy.ndarray` ring
buffer sized to hold exactly :data:`CHUNK_SECONDS` of audio. Each call
to :func:`record_chunk` waits until the buffer has accumulated a full
chunk worth of samples and then returns the snapshot as a contiguous
1-D ``float32`` array — so the next chunk can begin filling immediately
without copying.

Why a ring buffer instead of just calling :func:`sounddevice.rec`?
    * ``sd.rec`` blocks for the entire requested duration, which means a
      30 s call hides the first 29 s of speech from VAD and the worker
      can't react to ``controller.stop_event`` mid-recording.
    * The ring buffer lets the audio thread keep running while the
      asyncio worker swaps out a fresh ``record_chunk`` task; if the
      worker is briefly slow, audio is simply overwritten in place
      rather than queued forever.

Optional dependency policy — sounddevice + numpy may both be missing on
a fresh checkout. Imports are guarded; :func:`record_chunk` returns the
sentinel ``({"status": "missing_dep", "missing": [...]}, None)`` tuple so
the caller can log + skip without a hard crash. When the deps are
available the first call lazily starts the stream; :func:`stop_capture`
tears it down on shutdown.

TODO(pyproject) — add to ``[project.optional-dependencies]`` under a new
``audio`` extra (do NOT touch ``pyproject.toml`` from inside this
feature — see docs/AUDIO_DEPS.md)::

    audio = [
        "sounddevice>=0.4.6",
        "numpy>=1.24",
        "scipy>=1.10",
        "torch>=2.1",
        "silero-vad>=5.1",
        "pyloudnorm>=0.1.1",
        "encodec>=0.1.1",
        "ffmpeg-python>=0.2.0",
    ]
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover — type-checker only, never imported at runtime
    import numpy as np

log = get_logger("persona.audio.capture")

SAMPLE_RATE: int = 16_000
"""Standard ASR sample rate; matches silero-vad and Whisper expectations."""

CHANNELS: int = 1
"""Mono — speech models do not benefit from stereo and storage is halved."""

CHUNK_SECONDS: float = 30.0
"""One chunk = 30 s of audio. VAD then carves speech segments out of this."""

_BLOCK_SECONDS: float = 0.5
"""Hardware callback granularity. 0.5 s = ~8000 frames at 16 kHz, low jitter."""


try:  # numpy is needed for the ring buffer itself
    import numpy as np

    _NUMPY_OK = True
except ImportError as exc:  # pragma: no cover — exercised on bare installs
    np = None  # type: ignore[assignment]
    _NUMPY_OK = False
    log.warning("audio.capture.numpy_missing", error=str(exc))

try:  # sounddevice is the cross-platform PortAudio binding
    import sounddevice as sd

    _SOUNDDEVICE_OK = True
except (ImportError, OSError) as exc:  # OSError = PortAudio shared lib missing
    sd = None  # type: ignore[assignment, unused-ignore]
    _SOUNDDEVICE_OK = False
    log.warning("audio.capture.sounddevice_missing", error=str(exc))


def _missing_deps() -> list[str]:
    """Return the list of unimportable extras, empty if everything works."""
    missing: list[str] = []
    if not _NUMPY_OK:
        missing.append("numpy")
    if not _SOUNDDEVICE_OK:
        missing.append("sounddevice")
    return missing


@dataclass
class _RingBuffer:
    """Thread-safe ring of float32 samples.

    Producer (audio thread) calls :meth:`write`; consumer (asyncio task)
    calls :meth:`snapshot`. ``write`` never blocks — when the ring is
    full the oldest samples are dropped, which is the correct behaviour
    for a live capture: we'd rather lose stale audio than wedge the
    PortAudio callback.
    """

    capacity: int
    data: "np.ndarray"  # noqa: UP037 — string form so numpy import stays optional
    write_pos: int = 0
    filled: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def write(self, frames: np.ndarray) -> None:
        """Append ``frames`` to the ring, overwriting oldest samples if full."""
        n = int(frames.shape[0])
        if n == 0:
            return
        with self.lock:
            end = self.write_pos + n
            if end <= self.capacity:
                self.data[self.write_pos : end] = frames
            else:
                tail = self.capacity - self.write_pos
                self.data[self.write_pos :] = frames[:tail]
                self.data[: end - self.capacity] = frames[tail:]
            self.write_pos = end % self.capacity
            self.filled = min(self.capacity, self.filled + n)

    def _snapshot_unlocked(self) -> np.ndarray:
        """Build the snapshot copy **assuming the caller already holds the lock**.

        Split out from :meth:`snapshot` so a caller can take the snapshot
        and drain the buffer inside one critical section (see
        :meth:`snapshot_and_drain`) without releasing and re-acquiring the
        lock in between — that gap is where a PortAudio callback write
        would otherwise be silently discarded by the drain.
        """
        assert np is not None
        if self.filled < self.capacity:
            return self.data[: self.filled].copy()
        # Roll so the oldest sample sits at index 0.
        return np.concatenate(
            (self.data[self.write_pos :], self.data[: self.write_pos]),
            axis=0,
        ).copy()

    def snapshot(self) -> np.ndarray:
        """Return a contiguous copy of the most recent ``capacity`` samples.

        If the ring is not yet full the returned array is shorter — the
        caller should treat a short snapshot as "still warming up" and
        wait for the next tick.
        """
        with self.lock:
            return self._snapshot_unlocked()

    def snapshot_and_drain(self, target: int) -> "np.ndarray | None":
        """Atomically snapshot and, if full enough, drain the ring.

        Returns the snapshot copy when at least ``target`` samples are
        available and resets the ring in the **same** critical section so
        no callback write can land between the read and the reset (which
        would lose those frames). Returns ``None`` while still warming up,
        leaving the ring untouched so it keeps accumulating.
        """
        with self.lock:
            if self.filled < target:
                return None
            snap = self._snapshot_unlocked()
            # Drain so the next chunk doesn't overlap with this one.
            self.filled = 0
            self.write_pos = 0
            return snap


@dataclass
class _CaptureState:
    """Module-level singleton — the live PortAudio stream and its ring buffer.

    Wrapping the state in a mutable dataclass instance side-steps
    ``PLW0603`` (no ``global`` statement needed) while still keeping
    the visible API at function granularity. The instance itself is
    never reassigned — only its attributes mutate.
    """

    ring: _RingBuffer | None = None
    stream: Any = None
    started: bool = False


_state: _CaptureState = _CaptureState()


def _ensure_stream() -> None:
    """Lazily start the PortAudio input stream and ring buffer.

    Idempotent — repeated calls are no-ops after the first success. Any
    failure (no input device, permission denied) is logged and re-raised
    so :func:`record_chunk` can convert it into a ``missing_dep`` status.
    """
    if _state.started:
        return
    assert _NUMPY_OK and _SOUNDDEVICE_OK and np is not None and sd is not None
    capacity = int(SAMPLE_RATE * CHUNK_SECONDS)
    ring = _RingBuffer(capacity=capacity, data=np.zeros(capacity, dtype=np.float32))
    _state.ring = ring

    def _callback(
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        if status:
            log.debug("audio.capture.callback_status", status=str(status))
        # ``indata`` is (frames, channels); mono so we drop the channel axis.
        ring.write(indata[:, 0].astype(np.float32, copy=False))

    _state.stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=int(SAMPLE_RATE * _BLOCK_SECONDS),
        callback=_callback,
    )
    _state.stream.start()
    _state.started = True
    log.info(
        "audio.capture.started",
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
        chunk_seconds=CHUNK_SECONDS,
    )


def stop_capture() -> None:
    """Stop the PortAudio stream and release the ring buffer.

    Called from the worker's shutdown path so the next pytest run /
    headless boot starts from a clean state. Safe to call before
    :func:`_ensure_stream` has ever fired.
    """
    if _state.stream is not None:
        try:
            _state.stream.stop()
            _state.stream.close()
        except Exception as exc:
            log.warning("audio.capture.stop_failed", error=str(exc))
    _state.stream = None
    _state.ring = None
    _state.started = False
    log.info("audio.capture.stopped")


async def record_chunk() -> np.ndarray | dict[str, Any]:
    """Return one :data:`CHUNK_SECONDS`-long mono float32 chunk.

    Lazily starts the input stream on first call. Returns the sentinel
    ``{"status": "missing_dep", "missing": [...]}`` dict (not a numpy
    array) when sounddevice or numpy are unavailable, so the worker can
    pattern-match without a separate exception path.

    Blocks (cooperatively) until the ring buffer has at least
    :data:`CHUNK_SECONDS` worth of samples. Polling at 0.5 s keeps the
    asyncio event loop responsive to ``controller.stop_event``.
    """
    missing = _missing_deps()
    if missing:
        return {"status": "missing_dep", "missing": missing}

    try:
        await asyncio.to_thread(_ensure_stream)
    except Exception as exc:
        log.warning("audio.capture.stream_start_failed", error=str(exc))
        return {"status": "missing_dep", "missing": ["input_device"], "error": str(exc)}

    ring = _state.ring
    assert ring is not None and np is not None
    target = int(SAMPLE_RATE * CHUNK_SECONDS)
    while True:
        # Snapshot + size-check + drain happen inside one critical section
        # (``snapshot_and_drain``) so a PortAudio callback can't write
        # between the read and the reset and have those frames discarded.
        # A fresh 30-s window is forced — overlap would inflate
        # audio_segment row counts without adding speech information.
        snap = ring.snapshot_and_drain(target)
        if snap is not None:
            return snap[:target]
        await asyncio.sleep(0.5)


__all__ = [
    "CHANNELS",
    "CHUNK_SECONDS",
    "SAMPLE_RATE",
    "record_chunk",
    "stop_capture",
]
