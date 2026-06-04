"""Speech encoder cascade — Encodec (1.5 kbps) → Opus (4 kbps) → ffmpeg.

Three encoders are tried in priority order:

1. **Encodec** (Meta, neural codec, default ``preferred="encodec"``)
   ``encodec`` is a learned codec that beats Opus by 2-3 dB at 1.5 kbps
   on speech. We pin :func:`set_target_bandwidth(1.5)` for ~12 kB / 30 s
   of mono speech. The output bytes are the raw token stream; pair the
   file extension ``.encodec`` with the codec metadata so a future
   decoder pass knows which model to load.
2. **pyogg / opusfile** — pure-Python Opus encoder. Not always
   available on Windows wheels, so we try-import and fall through if
   missing.
3. **ffmpeg subprocess** — universal fallback. ``ffmpeg -i in.wav -c:a
   libopus -b:a 4000 -ar 16000 -ac 1 out.opus``. Requires ``ffmpeg`` on
   the system ``PATH`` but no Python deps.

The function returns ``(codec_name, encoded_bytes)``. When *all* three
fail we return ``("missing_dep", b"")`` so the worker can persist a row
with codec ``"missing_dep"`` for telemetry and move on.

Encodec's model is ~88 MB; downloaded on first use to ``~/.cache``.
The other encoders carry essentially zero state.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

log = get_logger("persona.audio.encode")

ENCODEC_BITRATE_KBPS: float = 1.5
"""Encodec sweet spot for speech — confirmed by Meta's own paper."""

OPUS_BITRATE_BPS: int = 4_000
"""Narrowband mono Opus — voice-call quality, ~15 kB / 30 s."""


try:
    import numpy as np

    _NUMPY_OK = True
except ImportError as exc:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _NUMPY_OK = False
    log.warning("audio.encode.numpy_missing", error=str(exc))


try:
    import torch

    _TORCH_OK = True
except ImportError:
    torch = None  # type: ignore[assignment, unused-ignore]
    _TORCH_OK = False


try:
    from encodec import (  # type: ignore[import-untyped, import-not-found, unused-ignore]
        EncodecModel,
    )

    _ENCODEC_OK = True
except ImportError:
    EncodecModel = None  # type: ignore[assignment, misc, unused-ignore]
    _ENCODEC_OK = False


# Cached Encodec model — populated on first :func:`_load_encodec` call.
# Wrapped in a single-element list so module-level reassignment is not
# required (avoids ``PLW0603``). The list itself is never reassigned.
_encodec_cache: list[Any] = [None]


def _load_encodec() -> Any:
    """Return the cached 24 kHz Encodec model, lazily loading on first call."""
    if _encodec_cache[0] is not None:
        return _encodec_cache[0]
    assert EncodecModel is not None and torch is not None
    model = EncodecModel.encodec_model_24khz()
    model.set_target_bandwidth(ENCODEC_BITRATE_KBPS)
    model.eval()
    _encodec_cache[0] = model
    log.info("audio.encode.encodec_loaded", bandwidth_kbps=ENCODEC_BITRATE_KBPS)
    return _encodec_cache[0]


def encode_segment(
    audio_arr: np.ndarray,
    sample_rate: int,
    *,
    preferred: str = "encodec",
) -> tuple[str, bytes]:
    """Encode one speech segment, returning ``(codec_name, encoded_bytes)``.

    ``codec_name`` is one of ``"encodec"`` / ``"opus"`` / ``"opus_ffmpeg"``
    / ``"missing_dep"``. The worker uses this to pick the file extension
    (``.encodec`` vs ``.opus``) and to populate the ``audio_segment.codec``
    column. On total failure returns ``("missing_dep", b"")`` rather than
    raising — speech capture should never bring down the whole worker.
    """
    if not _NUMPY_OK or np is None:
        log.warning("audio.encode.numpy_missing_at_call")
        return ("missing_dep", b"")

    order = _encoder_order(preferred)
    for candidate in order:
        try:
            if candidate == "encodec":
                encoded = _try_encodec(audio_arr, sample_rate)
            elif candidate == "opus":
                encoded = _try_pyogg(audio_arr, sample_rate)
            elif candidate == "opus_ffmpeg":
                encoded = _try_ffmpeg(audio_arr, sample_rate)
            else:
                continue
        except Exception as exc:
            log.warning(
                "audio.encode.candidate_failed",
                candidate=candidate,
                error=str(exc),
            )
            continue
        if encoded:
            log.debug(
                "audio.encode.success",
                codec=candidate,
                bytes=len(encoded),
                samples=int(audio_arr.shape[0]),
            )
            return (candidate, encoded)

    log.warning("audio.encode.all_failed", tried=order)
    return ("missing_dep", b"")


def _encoder_order(preferred: str) -> list[str]:
    """Build the ordered list of encoders to try, putting ``preferred`` first."""
    full = ["encodec", "opus", "opus_ffmpeg"]
    if preferred not in full:
        log.warning("audio.encode.unknown_preferred", preferred=preferred)
        return full
    return [preferred] + [c for c in full if c != preferred]


def _try_encodec(audio_arr: np.ndarray, sample_rate: int) -> bytes:
    """Encode via Meta's Encodec. Returns raw bytes of the token stream.

    Encodec expects (batch, channels, samples) at its native rate (24 kHz
    for the small model). We resample on the fly via :func:`numpy.interp`
    so the caller doesn't have to know the model's expectation.
    """
    if not (_ENCODEC_OK and _TORCH_OK):
        msg = "encodec or torch not installed"
        raise RuntimeError(msg)
    assert torch is not None and np is not None
    model = _load_encodec()
    target_sr = int(model.sample_rate)

    resampled = _resample_linear(audio_arr, src=sample_rate, dst=target_sr)
    tensor = torch.from_numpy(resampled).to(torch.float32).unsqueeze(0).unsqueeze(0)
    with torch.no_grad():
        frames = model.encode(tensor)
    # ``frames`` is a list of ``(codes, scale)`` tuples; serialise the
    # token tensor only — discard scales because Meta's reference
    # decoder reconstructs them from the bandwidth setting.
    chunks: list[bytes] = []
    for codes, _scale in frames:
        np_codes = codes.cpu().numpy().astype("int16")
        buffer = io.BytesIO()
        np.save(buffer, np_codes, allow_pickle=False)
        chunks.append(buffer.getvalue())
    # Prefix each chunk with a 4-byte length so the decoder can scan
    # the byte stream without external metadata.
    out = io.BytesIO()
    for chunk in chunks:
        out.write(len(chunk).to_bytes(4, "big"))
        out.write(chunk)
    return out.getvalue()


def _try_pyogg(
    audio_arr: np.ndarray,
    sample_rate: int,
) -> bytes:
    """Encode via pyogg if installed. Currently a placeholder pass-through.

    pyogg's Python API is unstable across versions and wheels are not
    published for Windows ARM; rather than carry a fragile encoder we
    raise ``NotImplementedError`` so the cascade falls through to the
    ffmpeg subprocess fallback. This keeps the codec-priority contract
    intact for future versions that ship a stable encoder.
    """
    try:
        import pyogg  # type: ignore[import-untyped, import-not-found, unused-ignore]  # noqa: F401, PLC0415 — optional + local
    except ImportError as exc:
        msg = "pyogg not installed"
        raise RuntimeError(msg) from exc
    msg = "pyogg encoder not yet wired — falling through to ffmpeg"
    raise NotImplementedError(msg)


def _try_ffmpeg(audio_arr: np.ndarray, sample_rate: int) -> bytes:
    """Pipe through an ``ffmpeg`` subprocess for libopus encoding.

    Universal fallback: works on any machine where ``ffmpeg`` is on the
    ``PATH``. We write the input WAV to a temp file (libopus' raw-PCM
    input mode is finicky across ffmpeg versions; WAV is bullet-proof)
    then read the encoded bytes back.
    """
    if shutil.which("ffmpeg") is None:
        msg = "ffmpeg not on PATH"
        raise RuntimeError(msg)
    assert np is not None

    with tempfile.TemporaryDirectory(prefix="persona-audio-") as td:
        in_path = Path(td) / "in.wav"
        out_path = Path(td) / "out.opus"
        _write_wav(in_path, audio_arr, sample_rate=sample_rate)
        cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(in_path),
            "-c:a",
            "libopus",
            "-b:a",
            str(OPUS_BITRATE_BPS),
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            str(out_path),
        ]
        result = subprocess.run(  # noqa: S603 — argv list, no shell expansion
            cmd,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            msg = f"ffmpeg exit {result.returncode}: {result.stderr.decode('utf-8', 'replace')}"
            raise RuntimeError(msg)
        return out_path.read_bytes()


def _write_wav(path: Path, audio: np.ndarray, *, sample_rate: int) -> None:
    """Write a mono 16-bit PCM WAV to ``path`` from a float32 array."""
    assert np is not None
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32_767.0).astype("<i2")
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(pcm.tobytes())


def _resample_linear(audio: np.ndarray, *, src: int, dst: int) -> np.ndarray:
    """Cheap linear-interpolation resampler — used only for the Encodec path.

    Good enough for speech feature extraction; the heavy lifting (anti-
    aliasing) was already done by the band-pass filter upstream. Using
    :mod:`scipy.signal.resample_poly` would be cleaner but pulls a hard
    scipy dep into the encoder, which we want to remain optional.
    """
    assert np is not None
    if src == dst:
        return audio.astype(np.float32, copy=False)
    n_dst = round(audio.shape[0] * dst / src)
    if n_dst <= 0:
        return audio.astype(np.float32, copy=False)
    x_src = np.linspace(0.0, 1.0, num=audio.shape[0], endpoint=False, dtype=np.float64)
    x_dst = np.linspace(0.0, 1.0, num=n_dst, endpoint=False, dtype=np.float64)
    return np.asarray(
        np.interp(x_dst, x_src, audio).astype(np.float32, copy=False),
        dtype=np.float32,
    )


__all__ = [
    "ENCODEC_BITRATE_KBPS",
    "OPUS_BITRATE_BPS",
    "encode_segment",
]
