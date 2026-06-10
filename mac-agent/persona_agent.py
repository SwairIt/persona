"""Persona Mac capture agent — daemon entry point.

This is the long-running process spawned by ``persona-agent run`` (and by
LaunchAgent ``com.persona.agent``).  Two async loops run inside one
``asyncio.run``:

* ``screen_loop``  — every ``capture.screen_interval`` seconds, grab the
  primary monitor via ``mss``, encode WebP at quality 60, dedup locally
  via pHash so identical frames never leave the laptop, then POST to
  ``/api/agent/screenshot``.

* ``audio_loop``   — continuous ``sounddevice`` capture at 16 kHz mono.
  A 30 s rolling buffer is fed into ``silero-vad``; only speech segments
  are encoded (Opus 4 kbps via ffmpeg subprocess, or optionally Encodec
  ~1.5 kbps if the optional dep is installed), transcribed locally with
  Whisper-small, then POSTed to ``/api/agent/audio-segment``.

Both loops respect a single ``paused`` flag toggled by ``SIGUSR1`` or by
the existence of the control file ``~/.persona-agent.paused``.  Both
loops back off + retry on network failure with capped exponential
backoff (2 → 4 → 8 → 16 → 32 → 60 s ceiling).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from config import AgentConfig, load_config

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger("persona_agent")

# T29 (2026-06-08) — bump on every release that changes agent behaviour so
# the server can detect outdated installs and surface an "update available"
# banner. Reported in the User-Agent of every request (ingest + sync). Keep
# in sync with ``LATEST_AGENT_VERSION`` in app/devices/agent_release.py.
# 1.13 = T28 workspace sync loop (sync_workspace_loop + device_token).
# 1.14 = T29 X-Agent-Token header (tunnel strips Authorization) + opt-in audio.
AGENT_VERSION = "1.16"


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _configure_logging(level_name: str) -> None:
    """Wire structlog with a rotating file handler.

    The agent runs as a launchd daemon; without rotation the
    ``StandardOutPath``-captured log file grows without bound. We cap
    the per-file size at 5 MB and keep 5 backups → ~25 MB total.

    The launchd plist still captures stdout/stderr (for early-boot
    errors before our handler is wired), but our own logger writes
    directly to the rotated file so the launchd capture stays tiny.
    """
    import logging
    from logging.handlers import RotatingFileHandler
    from pathlib import Path as _Path

    level = getattr(logging, level_name.upper(), logging.INFO)
    log_dir = _Path.home() / "Library" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "persona-agent.log"

    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _notify_user(title: str, message: str) -> None:
    """Best-effort macOS user notification via osascript.

    Surfaces the message in Notification Center so the user sees that
    something happened (crash, auth failure, upload stalled) without
    having to grep logs. Silently no-ops on non-macOS or when osascript
    is unavailable — this is a UX nicety, never load-bearing.
    """
    import subprocess

    try:
        # Escape both fields for AppleScript by stripping quotes/newlines.
        safe_title = title.replace('"', "").replace("\n", " ")[:80]
        safe_msg = message.replace('"', "").replace("\n", " ")[:200]
        script = (
            f'display notification "{safe_msg}" '
            f'with title "Persona Agent" '
            f'subtitle "{safe_title}"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            timeout=3,
            check=False,
            capture_output=True,
        )
    except Exception:  # noqa: BLE001
        # Notifications are advisory — never let them break the agent.
        pass


# Module-level latch so we only notify the user about auth failure
# once per process run — repeated 401s would otherwise pile up.
_AUTH_NOTIFIED: bool = False


def _set_auth_notified() -> None:
    """Flip the auth-notification latch; nullary so callers don't need ``global``."""
    global _AUTH_NOTIFIED  # noqa: PLW0603
    _AUTH_NOTIFIED = True


# --------------------------------------------------------------------------- #
# Shared mutable state
# --------------------------------------------------------------------------- #


@dataclass
class RuntimeState:
    """In-process shared state between the loops + signal handlers."""

    config: AgentConfig
    client: httpx.AsyncClient
    paused: bool = False
    # v1.39 — cached remote audio-pause state from /api/audio/mic.
    # A separate poller task refreshes this every 30 s. When True the
    # audio loop CLOSES its InputStream (so macOS turns off the
    # orange microphone indicator and stops draining battery) instead
    # of just dropping blocks.
    remote_audio_paused: bool = False
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    phash_history: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    agent_id: str = field(default_factory=lambda: f"mac-{uuid.uuid4().hex[:12]}")

    def should_pause(self) -> bool:
        """True if either SIGUSR1 toggled the flag or the control file exists.

        This is the GLOBAL pause — affects both screen and audio loops.
        For audio-only pause use :meth:`should_pause_audio`.
        """
        if self.paused:
            return True
        return self.config.pause_file.exists()

    def should_pause_audio(self) -> bool:
        """True when audio capture must stop — global pause OR remote mic pause."""
        return self.should_pause() or self.remote_audio_paused


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _server_endpoint(cfg: AgentConfig, path: str) -> str:
    """Concatenate ``server.url`` + ``path`` and normalise the slash."""
    base = str(cfg.server.url).rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def _auth_headers(cfg: AgentConfig) -> dict[str, str]:
    """Bearer token + agent fingerprint headers used on every request.

    T29 — the token is sent BOTH as ``Authorization: Bearer`` and as a
    custom ``X-Agent-Token`` header. Some tunnels/proxies (Microsoft Dev
    Tunnels) strip the standard Authorization header, which 401'd every
    screenshot/audio upload; the custom header survives the tunnel and the
    server accepts either.
    """
    token = cfg.server.token.get_secret_value()
    return {
        "Authorization": f"Bearer {token}",
        "X-Agent-Token": token,
        "User-Agent": f"persona-agent/{AGENT_VERSION} ({platform.system()} {platform.release()})",
    }


async def _post_with_backoff(
    state: RuntimeState,
    *,
    url: str,
    files: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    label: str,
) -> httpx.Response | None:
    """POST ``url`` with capped exponential backoff (2/4/8/16/32/60 s cap).

    Returns the final 2xx response, or ``None`` if ``state.stop`` was set
    before we succeeded.  Anything other than a transport error or 5xx
    response is treated as fatal-for-this-attempt and surfaced
    immediately — we do not retry past 4xx because the body the server
    rejected will never become acceptable.
    """
    delay = state.config.agent.backoff_initial_s
    cap = state.config.agent.backoff_max_s
    attempt = 0
    while not state.stop.is_set():
        attempt += 1
        try:
            response = await state.client.post(
                url,
                files=files,
                data=data,
                headers=_auth_headers(state.config),
                timeout=state.config.agent.request_timeout_s,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            logger.warning(
                "agent.upload.transport_error",
                label=label,
                attempt=attempt,
                delay=delay,
                error=str(exc),
            )
        else:
            if response.status_code < 400:
                return response
            if 400 <= response.status_code < 500:
                logger.error(
                    "agent.upload.client_error",
                    label=label,
                    status=response.status_code,
                    body=response.text[:500],
                )
                # 401 means the bearer token is wrong / revoked — the
                # user MUST act (re-run install.sh with a new token).
                # 403 means scope issue. Either way, notify once per
                # process so the user isn't blind to it.
                if response.status_code in (401, 403) and not _AUTH_NOTIFIED:
                    _notify_user(
                        "Token rejected",
                        f"{label} got HTTP {response.status_code} — re-run install.sh",
                    )
                    _set_auth_notified()
                return response
            logger.warning(
                "agent.upload.server_error",
                label=label,
                attempt=attempt,
                status=response.status_code,
                delay=delay,
            )
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=delay)
            return None
        except TimeoutError:
            pass
        delay = min(delay * 2, cap)
    return None


# --------------------------------------------------------------------------- #
# Screen loop
# --------------------------------------------------------------------------- #


def _capture_primary_frame() -> tuple[bytes, int, int, str]:
    """Return (webp_bytes, width, height, phash_hex) for the primary monitor.

    Runs in a thread (called via ``asyncio.to_thread``) because mss is
    synchronous and Pillow encoding holds the GIL.
    """
    import imagehash
    import mss
    from PIL import Image

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        image = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
        phash_hex = str(imagehash.phash(image, hash_size=8))
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=60, method=4)
        return buffer.getvalue(), shot.width, shot.height, phash_hex


def _phash_hamming(left: str, right: str) -> int:
    """Hamming distance between two hex pHash strings of equal length."""
    if len(left) != len(right):
        return max(len(left), len(right)) * 4
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _is_local_duplicate(state: RuntimeState, phash: str) -> bool:
    """True if ``phash`` is within the configured Hamming threshold of any
    recently-seen frame. Updates the history regardless so the window
    slides forward."""
    threshold = state.config.capture.screen_phash_threshold
    return any(_phash_hamming(phash, prior) <= threshold for prior in state.phash_history)


async def screen_loop(state: RuntimeState) -> None:
    """Capture + upload screenshots until ``state.stop`` is set."""
    interval = state.config.capture.screen_interval
    url = _server_endpoint(state.config, "/api/agent/screenshot")
    logger.info("agent.screen.loop_start", interval_s=interval, endpoint=url)

    while not state.stop.is_set():
        loop_started = time.monotonic()
        if state.should_pause():
            logger.debug("agent.screen.paused")
        else:
            try:
                webp_bytes, width, height, phash_hex = await asyncio.to_thread(
                    _capture_primary_frame,
                )
            except Exception as exc:  # any mss/PIL failure is reported and skipped
                logger.error("agent.screen.capture_failed", error=str(exc))
            else:
                if _is_local_duplicate(state, phash_hex):
                    logger.debug("agent.screen.local_dedup_skip", phash=phash_hex)
                else:
                    state.phash_history.append(phash_hex)
                    captured_at = datetime.now(UTC).isoformat()
                    files = {
                        "image": (
                            f"frame-{int(time.time())}.webp",
                            webp_bytes,
                            "image/webp",
                        ),
                    }
                    data = {
                        "captured_at": captured_at,
                        "width": str(width),
                        "height": str(height),
                        "phash": phash_hex,
                        "hostname": state.config.agent.hostname,
                        "agent_id": state.agent_id,
                    }
                    response = await _post_with_backoff(
                        state,
                        url=url,
                        files=files,
                        data=data,
                        label="screenshot",
                    )
                    if response is not None and response.status_code < 400:
                        logger.info(
                            "agent.screen.uploaded",
                            bytes=len(webp_bytes),
                            phash=phash_hex,
                            width=width,
                            height=height,
                        )

        # Sleep the remaining slice of the interval, but wake up early if
        # we are asked to stop.
        elapsed = time.monotonic() - loop_started
        remaining = max(0.0, interval - elapsed)
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=remaining)
            break  # stop requested
        except TimeoutError:
            continue

    logger.info("agent.screen.loop_stopped")


# --------------------------------------------------------------------------- #
# Audio loop
# --------------------------------------------------------------------------- #


class _VadDetector:
    """Lazy wrapper around silero-vad so import cost only hits the audio path."""

    def __init__(self, *, sample_rate: int, threshold: float) -> None:
        self.sample_rate = sample_rate
        self.threshold = threshold
        self._model: Any = None
        self._get_speech_timestamps: Any = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        # silero-vad ships a small helper API; we use the same call shape
        # the project has across releases.
        from silero_vad import get_speech_timestamps, load_silero_vad

        self._model = load_silero_vad()
        self._get_speech_timestamps = get_speech_timestamps

    def find_speech(self, samples: Any) -> list[dict[str, int]]:
        """Return ``[{'start': sample_idx, 'end': sample_idx}, ...]``."""
        self._ensure_model()
        import numpy as np

        # silero expects float32 mono in [-1, 1].
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        return list(
            self._get_speech_timestamps(
                samples,
                self._model,
                sampling_rate=self.sample_rate,
                threshold=self.threshold,
            )
        )


class _WebrtcVadDetector:
    """T29 — lightweight VAD via ``webrtcvad`` (tiny C lib, NO torch).

    Same ``find_speech(samples) -> [{'start','end'}]`` interface as
    :class:`_VadDetector` so the audio loop is backend-agnostic. This is
    the default detector: it lets "record everything I say" work with a
    few MB of deps instead of the ~2 GB silero/torch stack. Transcription
    is then optional (local Whisper) or done server-side.

    webrtcvad needs mono int16 PCM at 8/16/32/48 kHz and 10/20/30 ms
    frames. The agent captures float32 @ 16 kHz, which fits.
    """

    _FRAME_MS = 30

    def __init__(self, *, sample_rate: int, threshold: float) -> None:
        self.sample_rate = sample_rate
        self.aggressiveness = self._aggr(threshold)
        self._vad: Any = None

    @staticmethod
    def _aggr(threshold: float) -> int:
        """Map silero-style 0..1 onto webrtc 0..3 (higher = stricter)."""
        if threshold < 0.3:
            return 0
        if threshold < 0.5:
            return 1
        if threshold < 0.7:
            return 2
        return 3

    def _ensure(self) -> None:
        if self._vad is None:
            import webrtcvad

            self._vad = webrtcvad.Vad(self.aggressiveness)

    def find_speech(self, samples: Any) -> list[dict[str, int]]:
        """Return ``[{'start': sample_idx, 'end': sample_idx}, ...]``."""
        self._ensure()
        import numpy as np

        spf = int(self.sample_rate * self._FRAME_MS / 1000)
        if spf <= 0 or samples.size < spf:
            return []
        clipped = np.clip(samples, -1.0, 1.0)
        int16 = (clipped * 32767.0).astype(np.int16)
        total = int(len(int16) // spf)
        out: list[dict[str, int]] = []
        cur: int | None = None
        for i in range(total):
            start = i * spf
            frame = int16[start : start + spf].tobytes()
            speaking = self._vad.is_speech(frame, self.sample_rate)
            if speaking and cur is None:
                cur = start
            elif not speaking and cur is not None:
                out.append({"start": cur, "end": start})
                cur = None
        if cur is not None:
            out.append({"start": cur, "end": total * spf})
        return out


def _make_vad_detector(*, sample_rate: int, threshold: float) -> Any:
    """Pick the VAD backend: webrtcvad (light, no torch) if available,
    else silero (heavy). Lets installs without the 2 GB stack still
    capture speech."""
    try:
        import webrtcvad  # noqa: F401

        logger.info("agent.audio.vad_backend", backend="webrtcvad")
        return _WebrtcVadDetector(sample_rate=sample_rate, threshold=threshold)
    except ImportError:
        logger.info("agent.audio.vad_backend", backend="silero")
        return _VadDetector(sample_rate=sample_rate, threshold=threshold)


class _WhisperTranscriber:
    """Lazy Whisper loader. ``transcribe()`` is sync; call via to_thread."""

    def __init__(self, *, model_name: str, language: str | None) -> None:
        self.model_name = model_name
        self.language = language
        self._model: Any = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        import whisper

        self._model = whisper.load_model(self.model_name)

    def transcribe(self, samples: Any, sample_rate: int) -> str:
        self._ensure_model()
        import numpy as np

        audio = samples.astype(np.float32)
        if sample_rate != 16_000:
            # Whisper hard-codes 16 kHz internally.
            from scipy.signal import resample_poly

            audio = resample_poly(audio, 16_000, sample_rate).astype(np.float32)
        result = self._model.transcribe(
            audio,
            language=self.language,
            fp16=False,
        )
        text = result.get("text", "") if isinstance(result, dict) else ""
        return str(text).strip()


def _encode_opus_via_ffmpeg(samples: Any, sample_rate: int, bitrate: str) -> bytes:
    """Encode mono float32 PCM to Opus via an ffmpeg subprocess.

    We round-trip through a temporary WAV file rather than pipe raw PCM
    so users get a clearer error message if ffmpeg is missing — the wav
    detour is fast and ffmpeg is the only sane way to get Opus without
    pulling pyav.
    """
    import numpy as np
    from scipy.io import wavfile

    pcm16 = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16)
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = Path(tmp) / "in.wav"
        opus_path = Path(tmp) / "out.opus"
        wavfile.write(str(wav_path), sample_rate, pcm16)
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            "-c:a",
            "libopus",
            "-b:a",
            bitrate,
            "-application",
            "voip",
            str(opus_path),
        ]
        completed = subprocess.run(  # noqa: S603 - args list is fully controlled
            cmd,
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            msg = (
                "ffmpeg failed to encode Opus segment "
                f"(rc={completed.returncode}): {completed.stderr.decode('utf-8', 'replace')[:400]}"
            )
            raise RuntimeError(msg)
        return opus_path.read_bytes()


def _encode_with_encodec(samples: Any, sample_rate: int, bandwidth_kbps: float) -> bytes:
    """Encode via the optional ``encodec`` library; raises if it isn't installed."""
    try:
        import numpy as np
        import torch
        from encodec.model import EncodecModel
    except ImportError as exc:  # pragma: no cover - exercised only when extra installed
        msg = "encodec extra not installed; reinstall with 'pip install persona-agent[encodec]'"
        raise RuntimeError(msg) from exc

    model = EncodecModel.encodec_model_24khz()
    model.set_target_bandwidth(bandwidth_kbps)
    audio = samples.astype(np.float32)
    if sample_rate != model.sample_rate:
        from scipy.signal import resample_poly

        audio = resample_poly(audio, model.sample_rate, sample_rate).astype(np.float32)
    tensor = torch.from_numpy(audio).unsqueeze(0).unsqueeze(0)
    with torch.no_grad():
        encoded = model.encode(tensor)
    # Pack the codebook indices into raw bytes — the server-side decoder
    # mirrors this trivial layout.
    buf = io.BytesIO()
    for frame, scale in encoded:
        buf.write(frame.cpu().numpy().astype("int16").tobytes())
        if scale is not None:
            buf.write(scale.cpu().numpy().astype("float32").tobytes())
    return buf.getvalue()


async def remote_pause_poller(state: RuntimeState) -> None:
    """Poll the server's mic kill-switch every 30 s; update state.remote_audio_paused.

    v1.39 — when the user clicks 🎙 in the web UI, the SERVER flips
    ``kv.audio_capture_paused_live``. The Mac agent learns about it
    through this poller and reflects the change in ``state.remote_audio_paused``.
    The audio loop reads that flag every iteration and closes its
    ``sd.InputStream`` when True — that is what actually turns off the
    orange macOS microphone indicator and stops the battery drain.

    Failures (network, 401, server down) are silent: we just log debug
    and keep polling. We DO NOT change the cached value on error, so a
    temporarily-unreachable server doesn't flap pause state back to its
    last-known value.
    """
    url = _server_endpoint(state.config, "/api/audio/mic")
    interval_s = 30.0
    logger.info("agent.remote_pause.poller_start", endpoint=url, interval_s=interval_s)

    while not state.stop.is_set():
        try:
            response = await state.client.get(url, timeout=10.0)
            if response.status_code == 200:
                payload = response.json()
                new_value = bool(payload.get("paused", False))
                if new_value != state.remote_audio_paused:
                    logger.info("agent.remote_pause.changed", paused=new_value)
                    state.remote_audio_paused = new_value
            else:
                logger.debug(
                    "agent.remote_pause.unexpected_status",
                    status=response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 — never break the agent
            logger.debug("agent.remote_pause.poll_failed", error=str(exc))

        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue

    logger.info("agent.remote_pause.poller_stopped")


async def sync_heartbeat_loop(state: RuntimeState) -> None:
    """T7 (2026-06-07) — multi-device heartbeat + remote-control sync.

    Every 30 seconds the loop:
      1. POSTs ``/api/devices/heartbeat`` so the web /devices page can
         show "last seen N seconds ago" for this Mac.
      2. Reads the response's ``capture_paused`` flag (set on the web
         dashboard) and reflects it onto ``state.paused`` so the local
         capture loops honour the remote toggle. This is more general
         than the v1.39 mic-only poller — it covers SCREEN capture too.
      3. Reads ``capture_interval_seconds`` override (NULL = unchanged).
         The current agent doesn't yet rewire the screen loop on the fly
         to a new interval; that's a follow-up. For now we log the value
         so it's at least observable.

    Disabled (silent no-op) when ``device_token`` is missing from config
    — keeps single-device installs working without setup changes.
    """
    if state.config.server.device_token is None:
        logger.info("agent.sync_heartbeat.disabled_no_device_token")
        return

    # Lazy import: keep sync_client out of the import path when sync is off.
    from sync_client import SyncClient  # noqa: PLC0415

    client = SyncClient(
        server_url=str(state.config.server.url),
        device_token=state.config.server.device_token.get_secret_value(),
    )
    interval_s = 30.0
    logger.info("agent.sync_heartbeat.start", interval_s=interval_s)

    while not state.stop.is_set():
        try:
            resp = await client.heartbeat()
        except Exception as exc:  # noqa: BLE001 — never break the agent
            logger.debug("agent.sync_heartbeat.failed", error=str(exc))
            resp = {}

        if isinstance(resp, dict) and "device_id" in resp:
            new_paused = bool(resp.get("capture_paused", False))
            if new_paused != state.paused:
                logger.info(
                    "agent.sync_heartbeat.pause_changed",
                    paused=new_paused,
                )
                state.paused = new_paused
            interval_override = resp.get("capture_interval_seconds")
            if interval_override is not None:
                logger.debug(
                    "agent.sync_heartbeat.interval_override",
                    seconds=interval_override,
                )

        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue

    logger.info("agent.sync_heartbeat.stopped")


# --------------------------------------------------------------------------- #
# Workspace sync loop (T28)
# --------------------------------------------------------------------------- #


def _workspace_read_cursor(path: Path) -> int:
    """Read the persisted pull watermark. Missing / corrupt → 0."""
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _workspace_write_cursor(path: Path, cursor: int) -> None:
    """Persist the pull watermark so a restart doesn't re-pull everything."""
    try:
        path.write_text(str(int(cursor)), encoding="utf-8")
    except OSError as exc:
        logger.debug("agent.workspace_sync.cursor_write_failed", error=str(exc))


_SYNC_SKIP_NAMES = {".persona_sync_cursor", ".persona_sync_shas.json"}
_SYNC_MAX_BYTES = 2_000_000


def _content_sha(text: str) -> str:
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _load_shas(path: Path) -> dict[str, str]:
    import json  # noqa: PLC0415

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_shas(path: Path, shas: dict[str, str]) -> None:
    import json  # noqa: PLC0415

    try:
        path.write_text(json.dumps(shas), encoding="utf-8")
    except OSError as exc:
        logger.debug("agent.workspace_sync.sha_save_failed", error=str(exc))


def _apply_workspace_file(base: Path, entry: dict[str, Any], shas: dict[str, str]) -> bool:
    """Mirror one server file event into ``base``. Returns True if applied.
    Records the written content's sha in ``shas`` so the upstream scan
    won't re-upload what just came DOWN (breaks the ping-pong loop).

    Defense-in-depth sandbox: the resolved target must stay inside
    ``base`` even though the server already sandboxes paths.
    """
    rel = str(entry.get("relative_path", "")).strip().lstrip("/\\")
    op = str(entry.get("operation", "write"))
    if not rel:
        return False
    target = (base / rel).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        logger.warning("agent.workspace_sync.path_escape", rel=rel)
        return False

    if op == "delete":
        try:
            if target.is_file():
                target.unlink()
            shas.pop(rel, None)
            return True
        except OSError as exc:
            logger.debug("agent.workspace_sync.delete_failed", rel=rel, error=str(exc))
        return False

    content = entry.get("content")
    if content is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        shas[rel] = _content_sha(str(content))
        return True
    except OSError as exc:
        logger.warning("agent.workspace_sync.write_failed", rel=rel, error=str(exc))
        return False


async def _scan_and_push(base: Path, client: Any, shas: dict[str, str]) -> int:
    """T29 — UPSTREAM: upload locally-changed text files to the server so
    the AI can read/edit the user's real code. sha-guarded: a file whose
    sha already matches ``shas`` (unchanged, or just arrived from the
    server) is skipped — that's what stops the down/up ping-pong."""
    pushed = 0
    for f in base.rglob("*"):
        if not f.is_file() or f.name in _SYNC_SKIP_NAMES:
            continue
        try:
            if f.stat().st_size > _SYNC_MAX_BYTES:
                continue
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary / unreadable → skip
        rel = f.relative_to(base).as_posix()
        sha = _content_sha(text)
        if shas.get(rel) == sha:
            continue  # unchanged or came from server
        try:
            resp = await client.push_workspace_file(rel, text)
        except Exception as exc:  # noqa: BLE001 — never break the agent
            logger.debug("agent.workspace_sync.push_failed", rel=rel, error=str(exc))
            continue
        if isinstance(resp, dict) and resp.get("ok"):
            shas[rel] = sha
            pushed += 1
    return pushed


async def sync_workspace_loop(state: RuntimeState) -> None:
    """T28 — mirror AI-written workspace files into ``~/persona-workspace/``.

    The server marks ONE device per user as the "code write target"
    (chosen at /devices). Only that device's token is accepted by
    ``/api/workspace/sync``; every other device gets 403, which we treat
    as "not me yet" and quietly back off — the user can flip the choice
    at any time and this loop will start pulling within one interval.

    The pull watermark is persisted to ``~/persona-workspace/.persona_sync_cursor``
    so a restart resumes instead of re-pulling the whole history.

    Self-disables (silent no-op) when ``device_token`` is missing — keeps
    single-device installs working without setup changes.
    """
    if state.config.server.device_token is None:
        logger.info("agent.workspace_sync.disabled_no_device_token")
        return

    # Lazy import: keep sync_client off the import path when sync is off.
    from sync_client import SyncClient  # noqa: PLC0415

    client = SyncClient(
        server_url=str(state.config.server.url),
        device_token=state.config.server.device_token.get_secret_value(),
    )
    base = Path.home() / "persona-workspace"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("agent.workspace_sync.mkdir_failed", error=str(exc))
        return
    cursor_file = base / ".persona_sync_cursor"
    shas_file = base / ".persona_sync_shas.json"
    cursor = _workspace_read_cursor(cursor_file)
    shas = _load_shas(shas_file)
    interval_s = 20.0
    logger.info(
        "agent.workspace_sync.start",
        dir=str(base),
        cursor=cursor,
        interval_s=interval_s,
    )

    while not state.stop.is_set():
        is_target = True
        try:
            resp = await client.pull_workspace(since=cursor)
        except Exception as exc:  # noqa: BLE001 — never break the agent
            logger.debug("agent.workspace_sync.pull_failed", error=str(exc))
            resp = {}

        # --- DOWN: server → local mirror ---
        if isinstance(resp, dict) and resp.get("status") == 403:
            is_target = False
            logger.debug("agent.workspace_sync.not_target")
        elif isinstance(resp, dict) and "files" in resp:
            applied = 0
            for entry in resp.get("files") or []:
                if isinstance(entry, dict) and _apply_workspace_file(base, entry, shas):
                    applied += 1
            new_cursor = resp.get("cursor")
            if isinstance(new_cursor, int) and new_cursor >= cursor:
                cursor = new_cursor
                _workspace_write_cursor(cursor_file, cursor)
            if applied:
                logger.info(
                    "agent.workspace_sync.applied", count=applied, cursor=cursor
                )

        # --- UP: local edits → server (so the AI sees the user's code) ---
        if is_target:
            try:
                pushed = await _scan_and_push(base, client, shas)
                if pushed:
                    logger.info("agent.workspace_sync.pushed", count=pushed)
            except Exception as exc:  # noqa: BLE001
                logger.debug("agent.workspace_sync.scan_failed", error=str(exc))
            _save_shas(shas_file, shas)

        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue

    logger.info("agent.workspace_sync.stopped")


async def audio_loop(state: RuntimeState) -> None:
    """Capture mic audio, detect speech, encode + transcribe, upload.

    v1.39 — the ``sd.InputStream`` is now opened/closed dynamically based
    on :meth:`RuntimeState.should_pause_audio`. When the user pauses the
    mic from the web UI, the cached server flag flips, this loop exits
    the ``with stream:`` block, and macOS releases the microphone (the
    orange indicator disappears, battery drain stops). When the flag
    flips back, the loop reopens the stream within ``_PAUSE_POLL_S``.
    """
    import numpy as np
    import sounddevice as sd

    cfg = state.config.capture
    url = _server_endpoint(state.config, "/api/agent/audio-segment")
    sample_rate = cfg.audio_sample_rate
    buffer_samples = int(cfg.audio_buffer_seconds * sample_rate)
    block_size = sample_rate // 10  # 100 ms blocks
    logger.info(
        "agent.audio.loop_start",
        sample_rate=sample_rate,
        buffer_s=cfg.audio_buffer_seconds,
        endpoint=url,
        encoder=cfg.audio_encoder,
    )

    vad = _make_vad_detector(sample_rate=sample_rate, threshold=cfg.audio_vad_threshold)
    whisper = _WhisperTranscriber(
        model_name=cfg.whisper_model,
        language=cfg.whisper_language,
    )

    # Generous queue + drop-oldest semantics. Mic produces a 16k-sample
    # block every ~1s, VAD+encode+Whisper+HTTP can be much slower; we
    # would rather drop a stale tail-of-buffer than crash the callback
    # with QueueFull (which previously stopped the agent dead).
    pending_blocks: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=256)
    main_loop = asyncio.get_running_loop()

    def _enqueue(block: np.ndarray) -> None:
        if pending_blocks.full():
            try:
                pending_blocks.get_nowait()
            except asyncio.QueueEmpty:
                pass
            logger.warning("agent.audio.queue_full_drop")
        pending_blocks.put_nowait(block)

    def _callback(indata: Any, frames: int, _time_info: Any, status: Any) -> None:
        if status:
            logger.warning("agent.audio.stream_status", status=str(status))
        block = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        main_loop.call_soon_threadsafe(_enqueue, block)

    # v1.39 — how often to recheck pause state while idle (no stream open).
    _PAUSE_POLL_S = 5.0

    while not state.stop.is_set():
        # Idle while paused — DO NOT open the InputStream. That is what
        # keeps macOS quiet (no LED, no battery cost).
        if state.should_pause_audio():
            try:
                await asyncio.wait_for(state.stop.wait(), timeout=_PAUSE_POLL_S)
            except TimeoutError:
                pass
            continue

        # Open a fresh InputStream and run the capture inner-loop until
        # either the global stop fires OR the pause flag flips True.
        try:
            stream = sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=block_size,
                callback=_callback,
            )
        except Exception as exc:  # sounddevice raises platform-specific errors
            logger.error("agent.audio.stream_open_failed", error=str(exc))
            await asyncio.sleep(_PAUSE_POLL_S)
            continue

        # Drop any stale blocks that snuck into the queue during a
        # previous open-cycle so we don't replay old audio on resume.
        while not pending_blocks.empty():
            try:
                pending_blocks.get_nowait()
            except asyncio.QueueEmpty:
                break

        rolling = np.zeros(0, dtype=np.float32)
        last_speech_end = 0
        absolute_start = time.time()
        logger.info("agent.audio.stream_open")

        with stream:
            while not state.stop.is_set() and not state.should_pause_audio():
                try:
                    block = await asyncio.wait_for(pending_blocks.get(), timeout=1.0)
                except TimeoutError:
                    continue

                if state.should_pause():
                    # Drop accumulated audio rather than re-uploading it on resume.
                    rolling = np.zeros(0, dtype=np.float32)
                    last_speech_end = 0
                    absolute_start = time.time()
                    continue

                rolling = np.concatenate([rolling, block])

                # Keep the rolling buffer to ``audio_buffer_seconds`` of audio.
                if rolling.size > buffer_samples:
                    overflow = rolling.size - buffer_samples
                    rolling = rolling[overflow:]
                    last_speech_end = max(0, last_speech_end - overflow)
                    absolute_start += overflow / sample_rate

                if rolling.size < sample_rate * 2:
                    # Need at least ~2 s of audio before the VAD is informative.
                    continue

                try:
                    speech = await asyncio.to_thread(vad.find_speech, rolling)
                except Exception as exc:  # silero failures are logged + segment dropped
                    logger.error("agent.audio.vad_failed", error=str(exc))
                    continue

                # Process only segments that have ended (i.e. a clear silence
                # boundary after them); the trailing in-progress segment is
                # left in the buffer for the next iteration.
                min_speech = int(sample_rate * cfg.audio_min_speech_ms / 1000)
                min_silence = int(sample_rate * cfg.audio_min_silence_ms / 1000)
                settled: list[dict[str, int]] = []
                for seg in speech:
                    if seg["start"] < last_speech_end:
                        continue
                    if rolling.size - seg["end"] < min_silence:
                        continue
                    if seg["end"] - seg["start"] < min_speech:
                        continue
                    settled.append(seg)

                for seg in settled:
                    samples = rolling[seg["start"] : seg["end"]]
                    started_at = datetime.fromtimestamp(
                        absolute_start + seg["start"] / sample_rate,
                        tz=UTC,
                    )
                    duration = samples.size / sample_rate
                    await _process_speech_segment(
                        state,
                        samples=samples,
                        sample_rate=sample_rate,
                        started_at=started_at,
                        duration_s=duration,
                        url=url,
                        whisper=whisper,
                    )
                    last_speech_end = seg["end"]

        # v1.39 — exited the with-block → InputStream closed → macOS releases
        # the mic and the orange indicator turns off. Outer while loop will
        # decide whether to reopen the stream or keep sleeping.
        logger.info(
            "agent.audio.stream_closed",
            reason="paused" if state.should_pause_audio() else "stop",
        )

    logger.info("agent.audio.loop_stopped")


async def _process_speech_segment(
    state: RuntimeState,
    *,
    samples: Any,
    sample_rate: int,
    started_at: datetime,
    duration_s: float,
    url: str,
    whisper: _WhisperTranscriber,
) -> None:
    """Encode + transcribe a single segment, then upload it."""
    cfg = state.config.capture
    encoder = cfg.audio_encoder.lower()

    try:
        if encoder == "encodec":
            encoded = await asyncio.to_thread(
                _encode_with_encodec,
                samples,
                sample_rate,
                cfg.audio_encodec_bandwidth,
            )
            mime = "application/x-encodec"
            ext = "encodec"
        else:
            encoded = await asyncio.to_thread(
                _encode_opus_via_ffmpeg,
                samples,
                sample_rate,
                cfg.audio_opus_bitrate,
            )
            mime = "audio/ogg"
            ext = "opus"
    except Exception as exc:  # any encoder failure is logged + segment dropped
        logger.error("agent.audio.encode_failed", encoder=encoder, error=str(exc))
        return

    try:
        transcript = await asyncio.to_thread(whisper.transcribe, samples, sample_rate)
    except Exception as exc:  # whisper failure → upload audio without transcript
        logger.warning("agent.audio.transcribe_failed", error=str(exc))
        transcript = ""

    # T29 — field/key names must match the server's /api/agent/audio-segment
    # contract: file / captured_at / duration_seconds / codec. (Older agent
    # builds sent audio / started_at / duration_s / encoder, which 422'd.)
    files = {
        "file": (f"segment-{int(started_at.timestamp())}.{ext}", encoded, mime),
    }
    data = {
        "captured_at": started_at.isoformat(),
        "duration_seconds": f"{duration_s:.3f}",
        "codec": encoder,
        "sample_rate": str(sample_rate),
        "transcript": transcript,
        "hostname": state.config.agent.hostname,
        "agent_id": state.agent_id,
    }
    response = await _post_with_backoff(
        state,
        url=url,
        files=files,
        data=data,
        label="audio_segment",
    )
    if response is not None and response.status_code < 400:
        logger.info(
            "agent.audio.uploaded",
            bytes=len(encoded),
            duration_s=round(duration_s, 2),
            transcript_chars=len(transcript),
            encoder=encoder,
        )

        # T8 (2026-06-07) — fan the transcript out to other devices as a
        # sync note. The Mac uploaded the audio + transcript to the
        # server's ingest API just above; that path stores it in the
        # canonical audio_segment table. This additional emission turns
        # the transcript into a syncable note so it shows up on the
        # user's iPhone or Web UI immediately on the next sync pull,
        # without waiting for the audio_segment to be re-processed
        # cross-device. Best-effort — failure here never affects the
        # ingest above.
        if (
            transcript
            and len(transcript.strip()) >= 8
            and state.config.server.device_token is not None
        ):
            try:
                # Lazy import to avoid touching sync_client when no token.
                from sync_client import SyncClient  # noqa: PLC0415
                import time as _time  # noqa: PLC0415
                import uuid as _uuid  # noqa: PLC0415

                sync_cli = SyncClient(
                    server_url=str(state.config.server.url),
                    device_token=state.config.server.device_token.get_secret_value(),
                )
                await sync_cli.push_events(
                    [
                        {
                            "kind": "note",
                            "op": "insert",
                            "payload": {
                                "uuid": str(_uuid.uuid4()),
                                "body": transcript.strip(),
                                "title": (
                                    f"voice @ {started_at.strftime('%H:%M %m-%d')}"
                                ),
                                "source": "agent-voice",
                            },
                            "logical_clock": int(_time.time() * 1000),
                        }
                    ]
                )
                logger.info("agent.audio.synced_as_note", chars=len(transcript))
            except Exception as exc:  # noqa: BLE001
                logger.debug("agent.audio.sync_emit_failed", error=str(exc))


# --------------------------------------------------------------------------- #
# Signal handling
# --------------------------------------------------------------------------- #


def _install_signal_handlers(state: RuntimeState) -> None:
    """Wire SIGTERM/SIGINT to stop; SIGUSR1 to toggle pause.

    Some platforms (Windows in particular) lack SIGUSR1 — we install
    what we can and skip the rest silently.
    """
    loop = asyncio.get_running_loop()

    def _request_stop(*_args: object) -> None:
        logger.info("agent.signal.stop")
        state.stop.set()

    def _toggle_pause(*_args: object) -> None:
        state.paused = not state.paused
        logger.info("agent.signal.pause_toggled", paused=state.paused)

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _request_stop)

    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is not None:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sigusr1, _toggle_pause)


# --------------------------------------------------------------------------- #
# Async entry point
# --------------------------------------------------------------------------- #


async def _run_async(config: AgentConfig) -> int:
    """Spin up the HTTP client and both loops; return process exit code."""
    timeout = httpx.Timeout(config.agent.request_timeout_s, read=config.agent.request_timeout_s)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        state = RuntimeState(
            config=config,
            client=client,
            phash_history=deque(maxlen=config.capture.screen_phash_history),
        )
        _install_signal_handlers(state)
        logger.info(
            "agent.starting",
            server=str(config.server.url),
            hostname=config.agent.hostname,
            agent_id=state.agent_id,
            screen_enabled=config.capture.screen,
            audio_enabled=config.capture.audio,
        )

        tasks: list[asyncio.Task[None]] = []
        if config.capture.screen:
            tasks.append(asyncio.create_task(screen_loop(state), name="screen_loop"))
        if config.capture.audio:
            tasks.append(asyncio.create_task(audio_loop(state), name="audio_loop"))
            # v1.39 — only meaningful when audio is enabled; the poller
            # tells the audio loop to close its InputStream when the
            # server's web 🎙 toggle is set, freeing the macOS mic
            # indicator + stopping battery drain.
            tasks.append(
                asyncio.create_task(
                    remote_pause_poller(state),
                    name="remote_pause_poller",
                )
            )
        # T7 (2026-06-07) — multi-device sync heartbeat. Self-disables
        # when device_token is absent so single-device installs are
        # unaffected.
        tasks.append(
            asyncio.create_task(
                sync_heartbeat_loop(state),
                name="sync_heartbeat_loop",
            )
        )
        # T28 (2026-06-08) — pull AI-written workspace files down to
        # ~/persona-workspace/ when this device is the chosen code target.
        # Self-disables without a device_token, backs off on 403.
        tasks.append(
            asyncio.create_task(
                sync_workspace_loop(state),
                name="sync_workspace_loop",
            )
        )

        if not tasks:
            logger.warning("agent.no_loops_enabled — exiting")
            return 0

        await state.stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("agent.stopped")
        return 0


# --------------------------------------------------------------------------- #
# Sync wrapper used by the LaunchAgent / console_script
# --------------------------------------------------------------------------- #


def run(config_path: Path | str | None = None) -> int:
    """Synchronous run entry point — used by ``cli.py`` and the LaunchAgent.

    Returns a process exit code so callers can ``sys.exit`` on it.
    """
    config = load_config(config_path)
    _configure_logging(config.logging.level)
    logger.info(
        "agent.config_loaded",
        config_path=str(config.config_path) if config.config_path else "<env>",
    )
    _notify_user("Started", f"Sending to {config.server.url}")
    try:
        return asyncio.run(_run_async(config))
    except KeyboardInterrupt:
        logger.info("agent.keyboard_interrupt")
        return 0
    except Exception as exc:
        # Uncaught error → user gets a notification + the file log has
        # the full trace via the rotating handler. launchd will respawn
        # us (KeepAlive=Crashed) so this is the only crash signal that
        # reaches the human.
        logger.exception("agent.crashed", error=str(exc))
        _notify_user("Crashed — restarting", str(exc)[:200])
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script alias kept for direct ``python persona_agent.py`` use.

    The official CLI lives in ``cli.py`` (``persona-agent`` console_script).
    """
    from cli import main as cli_main

    return cli_main(argv)


# Re-export a couple of helpers the CLI calls directly so it does not
# have to depend on private names.
def signal_pause_toggle() -> bool:
    """Best-effort flip of the on-disk pause flag.

    Returns the new state (``True`` if the daemon is now paused).
    The daemon itself watches the file each loop iteration.
    """
    flag = Path.home() / ".persona-agent.paused"
    if flag.exists():
        flag.unlink(missing_ok=True)
        return False
    flag.touch()
    return True


def hostname_default() -> str:
    """Same logic the install script uses, without shelling out to scutil."""
    return socket.gethostname()


__all__ = [
    "RuntimeState",
    "audio_loop",
    "hostname_default",
    "main",
    "run",
    "screen_loop",
    "signal_pause_toggle",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
