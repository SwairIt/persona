"""Persona Mac agent configuration.

Loading order (highest priority first):

    1. ``--config /path/to/persona-agent.toml`` CLI flag (handled by cli.py).
    2. ``PERSONA_AGENT_CONFIG`` environment variable.
    3. ``~/.config/persona-agent.toml`` (the default written by install.sh).
    4. Environment variables ``PERSONA_SERVER_URL`` + ``PERSONA_AGENT_TOKEN``
       (the agentless fallback for CI / smoke tests — no toml required).

A minimal config is::

    [server]
    url   = "https://persona.example.com"
    token = "PA-xxxxxxxxxxxxxxxxx"

Everything else (capture cadence, audio sample rate, VAD thresholds, log
level, etc.) has a sensible default so the file shipped by the installer
stays short and human-readable.
"""

from __future__ import annotations

import contextlib
import os
import socket
import tomllib as _toml
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator

import platform_support as plat

# --------------------------------------------------------------------------- #
# Defaults (per-OS via platform_support: mac ~/.config, Windows %APPDATA%)
# --------------------------------------------------------------------------- #


DEFAULT_CONFIG_PATH = plat.config_path()
DEFAULT_PAUSE_FILE = plat.pause_file()
ENV_CONFIG_PATH = "PERSONA_AGENT_CONFIG"
ENV_SERVER_URL = "PERSONA_SERVER_URL"
ENV_AGENT_TOKEN = "PERSONA_AGENT_TOKEN"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class ServerSettings(BaseModel):
    """Server endpoint + tokens.

    ``token`` is the legacy bearer for every ingest upload (/api/agent/*).
    ``device_token`` (T6, optional) authenticates the multi-device sync
    endpoints (/api/sync/* + /api/devices/heartbeat) and is independent.
    When ``device_token`` is absent or empty, the sync loop in
    persona_agent stays disabled silently — single-device installs keep
    working without setup changes.
    """

    url: HttpUrl
    token: SecretStr
    device_token: SecretStr | None = None

    @field_validator("url")
    @classmethod
    def _strip_trailing_slash(cls, value: HttpUrl) -> HttpUrl:
        # HttpUrl already normalises this, but keep the field validator
        # so callers see the intent (server URL is always concatenated
        # with "/api/agent/...").
        return value


def _default_hostname() -> str:
    """Cross-platform hostname helper used as a pydantic default factory."""
    return socket.gethostname() or "unknown"


class AgentSettings(BaseModel):
    """Identity reported to the server with every upload."""

    hostname: str = Field(default_factory=_default_hostname)
    request_timeout_s: float = 20.0
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 60.0  # cap of the exponential-backoff schedule


class CaptureSettings(BaseModel):
    """Master switches + cadence."""

    screen: bool = True
    audio: bool = True

    # Screen.
    screen_interval: float = 30.0
    """Seconds between screen captures."""
    screen_webp_quality: int = 60
    screen_phash_size: int = 8
    screen_phash_threshold: int = 4
    """Hamming distance under which a frame is considered a near-duplicate
    of a recent one and skipped (local-only — server still dedups too)."""
    screen_phash_history: int = 200
    """Number of recent pHashes kept in memory for the local dedup check."""

    # Audio.
    audio_input_device: int | str | None = None
    """Optional input device index/name passed to sounddevice.InputStream.
    None = system default. Windows default-device selection can be flaky;
    run ``python -m sounddevice`` to list indices."""
    audio_sample_rate: int = 16_000
    audio_buffer_seconds: float = 30.0
    audio_vad_threshold: float = 0.5
    audio_min_speech_ms: int = 250
    audio_min_silence_ms: int = 500
    audio_opus_bitrate: str = "4k"
    audio_encodec_bandwidth: float = 1.5
    """Encodec target bitrate in kbps (only used when ``audio_encoder='encodec'``)."""
    audio_encoder: str = "opus"
    """Either ``"opus"`` (ffmpeg subprocess, always available) or
    ``"encodec"`` (only if the optional dep is installed)."""
    whisper_model: str = "small"
    """Whisper model name passed to ``whisper.load_model``."""
    whisper_language: str | None = None
    """If ``None``, Whisper auto-detects."""


class LoggingSettings(BaseModel):
    """Log level passed to structlog + stdlib logging."""

    level: str = "INFO"

    @field_validator("level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class AgentConfig(BaseModel):
    """Full agent configuration. Constructed via :func:`load_config`."""

    server: ServerSettings
    agent: AgentSettings = Field(default_factory=AgentSettings)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    pause_file: Path = DEFAULT_PAUSE_FILE
    config_path: Path | None = None
    """Where this config was loaded from. ``None`` when constructed from env."""


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file. Returns ``{}`` for an empty file.

    ``tomllib.load`` is statically typed to return ``dict[str, Any]`` so
    we trust that contract without an extra isinstance() guard.
    """
    with path.open("rb") as fh:
        return _toml.load(fh)


def _config_from_env() -> AgentConfig | None:
    """Build a config from PERSONA_SERVER_URL + PERSONA_AGENT_TOKEN.

    Returns ``None`` if either variable is missing.
    """
    url = os.environ.get(ENV_SERVER_URL)
    token = os.environ.get(ENV_AGENT_TOKEN)
    if not url or not token:
        return None
    return AgentConfig(server=ServerSettings(url=url, token=SecretStr(token)))  # type: ignore[arg-type]


def resolve_config_path(explicit: Path | str | None = None) -> Path | None:
    """Resolve which config file we should be reading.

    Order: ``explicit`` flag → ``PERSONA_AGENT_CONFIG`` env → default
    path (only if it actually exists).
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        return Path(env_path).expanduser()
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def load_config(explicit: Path | str | None = None) -> AgentConfig:
    """Load and validate the agent config.

    Resolution order matches :func:`resolve_config_path`; if no file is
    found we fall back to env vars. Raises ``FileNotFoundError`` with a
    helpful message if neither source is available.
    """
    path = resolve_config_path(explicit)
    if path is not None:
        if not path.is_file():
            msg = (
                f"persona-agent config not found at {path!s}. "
                "Run `persona-agent pair --server URL --token TOK` first."
            )
            raise FileNotFoundError(msg)
        data = _read_toml(path)
        cfg = AgentConfig.model_validate({**data, "config_path": path})
        return cfg

    env_cfg = _config_from_env()
    if env_cfg is not None:
        return env_cfg

    msg = (
        "no persona-agent config found. Either:\n"
        f"  • write {DEFAULT_CONFIG_PATH} via `persona-agent pair`, or\n"
        f"  • export {ENV_SERVER_URL} and {ENV_AGENT_TOKEN}."
    )
    raise FileNotFoundError(msg)


def write_config(
    *,
    server_url: str,
    token: str,
    path: Path | None = None,
    hostname: str | None = None,
) -> Path:
    """Write a minimal TOML config to *path* (default ``~/.config/persona-agent.toml``).

    The file is created with mode ``0600`` because it stores a bearer token.
    Existing files are overwritten — call sites that care about preserving
    user edits should diff first.
    """
    target = (path or DEFAULT_CONFIG_PATH).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    host = hostname or _default_hostname()
    # Strip a trailing slash so concatenation with /api/agent/... is predictable.
    server_url = server_url.rstrip("/")

    body = (
        "# Persona Mac agent configuration. Permissions: 0600.\n"
        "\n"
        "[server]\n"
        f'url   = "{server_url}"\n'
        f'token = "{token}"\n'
        "\n"
        "[agent]\n"
        f'hostname = "{host}"\n'
        "\n"
        "[capture]\n"
        "screen = true\n"
        "audio  = true\n"
        "\n"
        "[logging]\n"
        'level = "INFO"\n'
    )
    target.write_text(body, encoding="utf-8")
    with contextlib.suppress(OSError, NotImplementedError):  # pragma: no cover - non-POSIX
        target.chmod(0o600)
    return target


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_PAUSE_FILE",
    "ENV_AGENT_TOKEN",
    "ENV_CONFIG_PATH",
    "ENV_SERVER_URL",
    "AgentConfig",
    "AgentSettings",
    "CaptureSettings",
    "LoggingSettings",
    "ServerSettings",
    "load_config",
    "resolve_config_path",
    "write_config",
]
