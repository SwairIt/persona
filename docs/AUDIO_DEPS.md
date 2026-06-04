# Audio capture dependencies — v1.11 feature 1/3

The speech-only audio capture worker (`app.workers.audio_worker`) is
built on a stack of *optional* third-party packages. Every import is
guarded so the package is safe to load on a vanilla checkout — when a
backend is missing, the affected function returns a
`{"status": "missing_dep", "missing": [...]}` sentinel and the worker
logs a single warning instead of crashing.

The feature spec explicitly forbids touching `pyproject.toml` from
within feature 1/3, so this file is the **TODO note** for whoever wires
the deps into the project file in a follow-up patch.

## Add to `pyproject.toml`

Under `[project.optional-dependencies]`, add a new `audio` extra:

```toml
[project.optional-dependencies]
audio = [
    "sounddevice>=0.4.6",   # PortAudio binding — microphone capture (capture.py)
    "numpy>=1.24",          # ring buffer, array slicing (capture.py / preprocess.py / encode.py)
    "scipy>=1.10",          # butter / sosfiltfilt band-pass (preprocess.py)
    "pyloudnorm>=0.1.1",    # EBU R128 loudness normalisation (preprocess.py)
    "torch>=2.1",           # silero-vad backend + encodec model (vad.py / encode.py)
    "silero-vad>=5.1",      # PyPI helper — bundles ONNX + JIT models (vad.py)
    "encodec>=0.1.1",       # Meta neural codec @ 1.5 kbps (encode.py)
    "ffmpeg-python>=0.2.0", # optional — only used by the ffmpeg subprocess fallback
]
```

Install with:

```powershell
uv sync --extra audio
```

The worker also tries to call `ffmpeg` via subprocess as the universal
fallback encoder; this requires the **`ffmpeg` binary on `PATH`**, not
the Python package. On Windows the easiest path is
`winget install Gyan.FFmpeg`; on Linux `apt install ffmpeg` /
`brew install ffmpeg`.

## Per-file dep map

| File | Required deps | Behaviour when missing |
| --- | --- | --- |
| `app/audio/capture.py` | `numpy`, `sounddevice` | `record_chunk()` returns `{"status": "missing_dep", "missing": ["numpy", "sounddevice"]}` |
| `app/audio/vad.py` | `torch`, `silero-vad` | `detect_speech_segments()` returns `{"status": "missing_dep", "missing": [...]}` |
| `app/audio/preprocess.py` | `numpy`, `scipy`, `pyloudnorm` | gracefully degrades — drops band-pass without scipy, falls back to peak-normalise -3 dBFS without pyloudnorm |
| `app/audio/encode.py` | `numpy` + at least one of (`encodec`+`torch`, `pyogg`, ffmpeg-on-PATH) | returns `("missing_dep", b"")` if every encoder fails |

## Runtime model downloads

* **silero-vad** — fetches ~1.8 MB of ONNX/JIT weights on first call;
  cached under the platform-specific torch hub directory (`~/.cache/torch/hub/`).
* **Encodec** — downloads the ~88 MB 24 kHz model from Meta's HF mirror
  on first encode. Same cache directory.

Both downloads are silent and one-time; the worker still functions
without internet on subsequent runs.

## Privacy posture

`settings.audio_capture_enabled` defaults to **False**. Even with every
dep installed, the worker will not touch the microphone until the user
explicitly opts in. This mirrors the established pattern for
`clipboard_history_enabled`.
