"""Settings page for the audio capture pipeline.

v1.11 feature 3/3, route 3 of 5. Exposes an operator-editable form
backed by the same ``kv_settings`` table the rest of the runtime
settings use (theme, compact mode, retention overrides, ...). The
form covers six knobs:

================================  ==================================================
Form field                        kv_settings row
================================  ==================================================
``audio_capture_enabled`` (bool)  ``audio_capture_enabled``  (``"1"`` / ``"0"``)
``codec`` (select)                ``audio_codec``            (string, whitelist)
``bitrate`` (number, kbps)        ``audio_bitrate``          (int as string)
``vad_threshold`` (number, 0-1)   ``audio_vad_threshold``    (float as string)
``retention_hot_days`` (int)      ``audio_retention_hot_days`` (int as string)
``whisper_model`` (select)        ``audio_whisper_model``    (string, whitelist)
================================  ==================================================

All six values are written through :func:`app.storage.repository.set_kv`
in a single connection so a partial save is impossible — either every
row reflects the form, or nothing changes.

v1.30: registered in :mod:`app.web.main` (the older "deliberately not
registered" docstring was stale — coordinator picks every route module
up automatically once it ships).
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

log = get_logger("persona.audio.web")

router = APIRouter(tags=["audio-settings"])

# ----- kv_settings row names ---------------------------------------------
# All keys are namespaced under the ``audio_`` prefix so a future
# ``list_kv`` consumer can grep them out as one block.

_KV_CAPTURE_ENABLED: Final[str] = "audio_capture_enabled"
_KV_CODEC: Final[str] = "audio_codec"
_KV_BITRATE: Final[str] = "audio_bitrate"
_KV_VAD_THRESHOLD: Final[str] = "audio_vad_threshold"
_KV_RETENTION_HOT_DAYS: Final[str] = "audio_retention_hot_days"
_KV_WHISPER_MODEL: Final[str] = "audio_whisper_model"

# ----- whitelists ---------------------------------------------------------

# Codec options surfaced in the dropdown. The capture worker writes the
# bytes itself, so an operator picking ``opus`` here means "next take
# encodes as opus" — *existing* segments keep whatever codec column
# value they were written with.
_CODEC_OPTIONS: Final[tuple[str, ...]] = ("opus", "ogg", "mp3", "wav", "flac", "aac")
_CODEC_DEFAULT: Final[str] = "opus"

# Whisper model options. ``faster-whisper`` ships these names; the
# small/medium/large tiers trade transcription quality against RAM and
# wall-clock time. Default ``base`` matches what the worker boots with
# when the kv row is missing — see v1.11 feature 2/3.
_WHISPER_MODEL_OPTIONS: Final[tuple[str, ...]] = (
    "tiny",
    "base",
    "small",
    "medium",
    "large",
)
_WHISPER_MODEL_DEFAULT: Final[str] = "base"

# ----- numeric defaults / clamps ------------------------------------------

# Sensible bitrate floor / ceiling for the codec set above. 8 kbps is
# already painful for opus and *unusable* for mp3; 320 kbps is the
# upper bound the worker honours. Out-of-range form input is clamped
# rather than rejected because the user just typed a number, not
# malice.
_BITRATE_MIN: Final[int] = 8
_BITRATE_MAX: Final[int] = 320
_BITRATE_DEFAULT: Final[int] = 24

_VAD_THRESHOLD_MIN: Final[float] = 0.0
_VAD_THRESHOLD_MAX: Final[float] = 1.0
_VAD_THRESHOLD_DEFAULT: Final[float] = 0.5

_RETENTION_HOT_DAYS_MIN: Final[int] = 1
_RETENTION_HOT_DAYS_MAX: Final[int] = 3650
_RETENTION_HOT_DAYS_DEFAULT: Final[int] = 30


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _parse_checkbox(raw: str | None) -> bool:
    """HTML checkboxes only POST a value when ticked.

    An absent / empty field becomes ``False``; anything matching the
    usual truthy bag becomes ``True``. Matches the convention used by
    :mod:`app.web.routes.settings` so an operator's mental model is
    consistent across the two settings pages.
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_codec(raw: str | None) -> str:
    """Normalise the codec value or fall back to the default."""
    if raw is None:
        return _CODEC_DEFAULT
    normalised = raw.strip().lower()
    if normalised not in _CODEC_OPTIONS:
        return _CODEC_DEFAULT
    return normalised


def _parse_whisper_model(raw: str | None) -> str:
    """Normalise the Whisper-model value or fall back to the default."""
    if raw is None:
        return _WHISPER_MODEL_DEFAULT
    normalised = raw.strip().lower()
    if normalised not in _WHISPER_MODEL_OPTIONS:
        return _WHISPER_MODEL_DEFAULT
    return normalised


def _parse_int(raw: str | None, default: int, lo: int, hi: int) -> int:
    """Coerce ``raw`` to an int and clamp to ``[lo, hi]``.

    Out-of-range / unparseable input falls back to ``default`` so the
    POST handler never raises on form data — a redirect-after-save
    that 500s would lose the operator's other field edits.
    """
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _parse_float(raw: str | None, default: float, lo: float, hi: float) -> float:
    """Coerce ``raw`` to a float and clamp to ``[lo, hi]``.

    Same forgiving contract as :func:`_parse_int`.
    """
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ---------------------------------------------------------------------------
# Read helpers used by the GET page
# ---------------------------------------------------------------------------


def _read_capture_enabled(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_codec(raw: str | None) -> str:
    if raw is None:
        return _CODEC_DEFAULT
    normalised = raw.strip().lower()
    if normalised not in _CODEC_OPTIONS:
        return _CODEC_DEFAULT
    return normalised


def _read_whisper_model(raw: str | None) -> str:
    if raw is None:
        return _WHISPER_MODEL_DEFAULT
    normalised = raw.strip().lower()
    if normalised not in _WHISPER_MODEL_OPTIONS:
        return _WHISPER_MODEL_DEFAULT
    return normalised


def _read_int(raw: str | None, default: int, lo: int, hi: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    if value < lo or value > hi:
        return default
    return value


def _read_float(raw: str | None, default: float, lo: float, hi: float) -> float:
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return default
    if value < lo or value > hi:
        return default
    return value


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/settings/audio", response_class=HTMLResponse)
async def audio_settings_page(request: Request) -> HTMLResponse:
    """Render the audio settings form, pre-populated from ``kv_settings``."""
    async with get_connection() as conn:
        capture_raw = await get_kv(conn, _KV_CAPTURE_ENABLED)
        codec_raw = await get_kv(conn, _KV_CODEC)
        bitrate_raw = await get_kv(conn, _KV_BITRATE)
        vad_raw = await get_kv(conn, _KV_VAD_THRESHOLD)
        retention_raw = await get_kv(conn, _KV_RETENTION_HOT_DAYS)
        whisper_raw = await get_kv(conn, _KV_WHISPER_MODEL)

    capture_enabled = _read_capture_enabled(capture_raw)
    codec = _read_codec(codec_raw)
    bitrate = _read_int(bitrate_raw, _BITRATE_DEFAULT, _BITRATE_MIN, _BITRATE_MAX)
    vad_threshold = _read_float(
        vad_raw, _VAD_THRESHOLD_DEFAULT, _VAD_THRESHOLD_MIN, _VAD_THRESHOLD_MAX
    )
    retention_hot_days = _read_int(
        retention_raw,
        _RETENTION_HOT_DAYS_DEFAULT,
        _RETENTION_HOT_DAYS_MIN,
        _RETENTION_HOT_DAYS_MAX,
    )
    whisper_model = _read_whisper_model(whisper_raw)

    log.info(
        "audio.settings.page",
        capture_enabled=capture_enabled,
        codec=codec,
        bitrate=bitrate,
        vad_threshold=vad_threshold,
        retention_hot_days=retention_hot_days,
        whisper_model=whisper_model,
    )

    return templates.TemplateResponse(
        request,
        "audio_settings.html",
        {
            "title": "Audio settings",
            "active_nav": "settings",
            "capture_enabled": capture_enabled,
            "codec": codec,
            "codec_options": _CODEC_OPTIONS,
            "bitrate": bitrate,
            "bitrate_min": _BITRATE_MIN,
            "bitrate_max": _BITRATE_MAX,
            "vad_threshold": vad_threshold,
            "vad_threshold_min": _VAD_THRESHOLD_MIN,
            "vad_threshold_max": _VAD_THRESHOLD_MAX,
            "retention_hot_days": retention_hot_days,
            "retention_hot_days_min": _RETENTION_HOT_DAYS_MIN,
            "retention_hot_days_max": _RETENTION_HOT_DAYS_MAX,
            "whisper_model": whisper_model,
            "whisper_model_options": _WHISPER_MODEL_OPTIONS,
        },
    )


@router.post("/settings/audio", response_class=HTMLResponse)
async def audio_settings_save(
    request: Request,
    audio_capture_enabled: str = Form(default=""),
    codec: str = Form(default=""),
    bitrate: str = Form(default=""),
    vad_threshold: str = Form(default=""),
    retention_hot_days: str = Form(default=""),
    whisper_model: str = Form(default=""),
) -> RedirectResponse:
    """Persist every audio knob to ``kv_settings`` in one connection.

    Reuses a single :func:`get_connection` so the six ``set_kv`` calls
    share one WAL transaction — a partial save (some rows written,
    others skipped because of a transient error) is impossible.
    """
    capture_value = _parse_checkbox(audio_capture_enabled)
    codec_value = _parse_codec(codec)
    bitrate_value = _parse_int(bitrate, _BITRATE_DEFAULT, _BITRATE_MIN, _BITRATE_MAX)
    vad_value = _parse_float(
        vad_threshold,
        _VAD_THRESHOLD_DEFAULT,
        _VAD_THRESHOLD_MIN,
        _VAD_THRESHOLD_MAX,
    )
    retention_value = _parse_int(
        retention_hot_days,
        _RETENTION_HOT_DAYS_DEFAULT,
        _RETENTION_HOT_DAYS_MIN,
        _RETENTION_HOT_DAYS_MAX,
    )
    whisper_value = _parse_whisper_model(whisper_model)

    async with get_connection() as conn:
        await set_kv(conn, _KV_CAPTURE_ENABLED, "1" if capture_value else "0")
        await set_kv(conn, _KV_CODEC, codec_value)
        await set_kv(conn, _KV_BITRATE, str(bitrate_value))
        # Float-as-string keeps the kv format consistent across rows.
        # The reader (:func:`_read_float`) parses it back symmetrically.
        await set_kv(conn, _KV_VAD_THRESHOLD, f"{vad_value:.4f}")
        await set_kv(conn, _KV_RETENTION_HOT_DAYS, str(retention_value))
        await set_kv(conn, _KV_WHISPER_MODEL, whisper_value)

    log.info(
        "audio.settings.save",
        capture_enabled=capture_value,
        codec=codec_value,
        bitrate=bitrate_value,
        vad_threshold=vad_value,
        retention_hot_days=retention_value,
        whisper_model=whisper_value,
    )

    return RedirectResponse(url="/settings/audio", status_code=303)


__all__ = ["router"]
