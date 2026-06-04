"""Typed application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PERSONA_",
        case_sensitive=False,
        extra="ignore",
    )

    # v1.18 — defaults live in the user's home, not the repo working
    # directory. This means ``rm -rf Persona/`` or ``git pull`` cannot
    # accidentally wipe the entire memory archive. Override via env if
    # you want a different mount (e.g. an external drive). ``~`` is
    # expanded by the field validator below so any user can copy the
    # default without filling in their home path manually.
    data_dir: Path = Field(default=Path("~/.persona"))
    db_path: Path = Field(default=Path("~/.persona/persona.db"))
    thumbnails_dir: Path = Field(default=Path("~/.persona/thumbnails"))

    # v1.13 — tightened defaults to fit the 25 MB/day budget (see
    # docs/STORAGE_BUDGET_DESIGN.md §10).
    # v1.16 — restored ~10 frames/min when active (was 8s = 7.5/min).
    # User feedback: stretched cadence lost too much context; dedup +
    # smart_gap + budget enforcer hold the 25 MB cap on their own.
    capture_interval_seconds: float = Field(default=6.0, ge=0.5, le=60.0)
    thumbnail_quality: int = Field(default=35, ge=10, le=100)
    thumbnail_max_width: int = Field(default=640, ge=320, le=3840)
    dedup_hamming_threshold: int = Field(default=8, ge=0, le=64)
    retention_days: int = Field(default=180, ge=1, le=3650)
    idle_threshold_seconds: float = Field(default=300.0, ge=10.0)
    lock_aware_pause_enabled: bool = Field(default=True)

    smart_thumbnail: bool = Field(default=True)
    smart_min_gap_seconds: float = Field(default=300.0, ge=0.0)
    # Legacy nominal thumbnail-write cap. Superseded by daily_budget_mb +
    # budget_enforcer_enabled (v1.13). Kept for backward compat; consulted
    # only when budget_enforcer_enabled is False.
    daily_size_budget_mb: float = Field(default=4.0, ge=0.1, le=10240.0)
    # v1.13 — pull warm tier inward so yesterday's frames downsize today.
    tier_warm_after_days: int = Field(default=1, ge=1, le=3650)
    tier_cold_after_days: int = Field(default=30, ge=1, le=3650)
    tier_warm_thumbnail_width: int = Field(default=256, ge=64, le=3840)
    tier_warm_thumbnail_quality: int = Field(default=25, ge=10, le=100)
    tiered_retention: bool = Field(default=True)
    archive_after_days: int = Field(default=180, ge=30, le=3650)
    archive_enabled: bool = Field(default=False)
    multi_monitor: bool = Field(default=False)
    theme: str = Field(default="dark")
    auto_digest_enabled: bool = Field(default=False)
    auto_digest_hour_local: int = Field(default=22, ge=0, le=23)
    weekly_digest_enabled: bool = Field(default=False)
    weekly_digest_hour_local: int = Field(default=8, ge=0, le=23)
    monthly_digest_enabled: bool = Field(default=False)
    monthly_digest_hour_local: int = Field(default=9, ge=0, le=23)
    daily_email_enabled: bool = Field(default=False)
    daily_email_hour_local: int = Field(default=8, ge=0, le=23)
    weekly_stats_email_enabled: bool = Field(default=False)
    weekly_stats_email_hour_local: int = Field(default=9, ge=0, le=23)

    # v0.72 — day-end auto-summary. When True, a 30-min polling worker
    # generates today's ``day_tldr`` row a bit before midnight so the
    # next morning's ``/timeline/{day}`` and ``/digest`` loads do not
    # have to wait on a synchronous LLM call. ``hour_local`` is the
    # local hour after which (specifically: at HH:30) the worker may
    # fire — default 23, i.e. the TL;DR is primed after 23:30 local.
    # The underlying ``summarise_day_tldr`` is idempotent so duplicate
    # ticks within the window are no-ops.
    day_end_summary_enabled: bool = Field(default=False)
    day_end_summary_hour_local: int = Field(default=23, ge=0, le=23)

    # v0.59 — anti-FOMO digest mode. When True, the daily and weekly
    # LLM digests instruct the model to produce a qualitative,
    # theme-only retrospective without mentioning shot counts,
    # percentages, time-spent figures, or any productivity-style
    # metric. Default False keeps the existing behaviour for
    # everyone who has not opted in.
    anti_fomo_digest: bool = Field(default=False)

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765, ge=1, le=65535)
    log_level: str = Field(default="INFO")

    tesseract_path: Path | None = Field(default=None)
    tesseract_langs: str = Field(default="eng+rus")
    ocr_enabled: bool = Field(default=False)
    image_blur_enabled: bool = Field(default=False)

    byo_api_key: str = Field(default="")
    byo_api_provider: str = Field(default="")

    # v0.55 — OCR fallback via multimodal LLM vision. When enabled (default
    # False) and ``byo_api_provider == "anthropic"``, low-confidence /
    # empty-text shots can be re-transcribed by sending the thumbnail
    # bytes as a base64 image to the BYO LLM. The result is cached in a
    # dedicated ``ocr_text_vision`` column on the ``screenshots`` table
    # — Tesseract's ``ocr_text`` is never overwritten so the two
    # signals stay independent. Defaults False because vision calls are
    # markedly more expensive than text-only completions and the user
    # must explicitly opt in.
    llm_vision_enabled: bool = Field(default=False)

    embeddings_enabled: bool = Field(default=False)
    embeddings_model: str = Field(default="intfloat/multilingual-e5-small")
    embeddings_batch_size: int = Field(default=16, ge=1, le=128)
    embeddings_min_text_length: int = Field(default=20, ge=0, le=10000)

    battery_aware_enabled: bool = Field(default=True)
    battery_capture_multiplier: float = Field(default=3.0, gt=0.0, le=20.0)
    battery_critical_pct: int = Field(default=15, ge=1, le=50)

    adaptive_cadence_enabled: bool = Field(default=True)
    # v1.16 — adaptive_min matches the base interval so active-typing
    # doesn't slow capture below 10/min. adaptive_max keeps the v1.13
    # widened idle bound (15 min between captures when the user is afk
    # within the still-not-yet-idle window).
    adaptive_min_seconds: int = Field(default=6, ge=5, le=300)
    adaptive_max_seconds: int = Field(default=900, ge=60, le=3600)

    # v1.13 — Storage-budget enforcer. The total daily on-disk growth
    # target. The capture loop and audio worker check projected EoD usage
    # and throttle their own behaviour when projection exceeds the cap.
    # See docs/STORAGE_BUDGET_DESIGN.md for the throttle level table.
    daily_budget_mb: float = Field(default=25.0, ge=1.0, le=10240.0)
    budget_enforcer_enabled: bool = Field(default=True)
    budget_throttle_aggressiveness: str = Field(default="mild")

    # v0.35 — opt-in clipboard history capture. When True, a background
    # worker polls the OS clipboard every ~2s and stores each new text
    # snippet in ``clipboard_event``. Default False for privacy — the
    # user must explicitly turn it on.
    clipboard_history_enabled: bool = Field(default=False)

    # v0.37 — markdown notes inbox. When ``inbox_enabled`` (default
    # True) a background worker scans ``inbox_path`` every ~30s for
    # ``*.md`` files, imports each into the ``notes`` table, and moves
    # the file into ``processed/`` (or ``failed/`` with a sibling
    # ``.error.txt`` on parse failure). Default location is
    # ``./data/inbox`` so the inbox lives under the existing data tree.
    inbox_enabled: bool = Field(default=True)
    inbox_path: Path = Field(default=Path("~/.persona/inbox"))

    # v0.34 — when False (default) ``/api/*`` endpoints stay open to the
    # local UI exactly as before; bearer auth only kicks in for requests
    # that carry an ``Authorization: Bearer …`` header. Flip to True to
    # *require* a valid token on every ``/api/*`` call.
    api_auth_required: bool = Field(default=False)

    # v0.85 — when False (default) ``/feeds/*`` RSS/Atom endpoints keep
    # their legacy open-access behaviour, so existing feed-reader
    # subscriptions keep working across the upgrade. Flip to True to
    # require a ``?token=…`` query param matched against the
    # ``feed_token`` table (migration 074); shareable per-feed tokens
    # are minted on the ``/settings/feed-tokens`` page.
    feed_auth_required: bool = Field(default=False)

    # v0.40 — soft-delete recycle bin. Screenshots and notes deleted
    # via the UI go into ``recycle_bin`` first; the retention worker
    # hard-deletes anything older than this many days (and unlinks any
    # thumbnail file) once per loop iteration.
    recycle_retention_days: int = Field(default=7, ge=1, le=90)

    # v0.76 — nightly encrypted DB backup scheduler. When
    # ``auto_backup_enabled`` is True the ``auto_backup_scheduler``
    # worker polls every 30 minutes and, when local-time hour matches
    # ``auto_backup_hour_local`` (default 03:00), invokes the v0.23
    # backup CLI logic (:func:`app.backup.snapshot.create_backup`) to
    # write an encrypted snapshot into ``auto_backup_path``. The
    # passphrase is fetched from the v0.33 vault under the key
    # ``auto_backup_password`` — the worker is silent (logs + skips)
    # when the vault master password is unavailable, the row is
    # missing, or the optional ``cryptography`` dep is not installed.
    # Files older than ``auto_backup_keep_days`` are pruned at the end
    # of each successful run. Default off so existing users are
    # unaffected.
    auto_backup_enabled: bool = Field(default=False)
    auto_backup_hour_local: int = Field(default=3, ge=0, le=23)
    auto_backup_path: Path = Field(default=Path("~/.persona/backups"))
    auto_backup_keep_days: int = Field(default=14, ge=1, le=3650)

    # v0.90 — capture-rate guard. The capture loop counts screenshots
    # written in the trailing 60-minute window at the start of each
    # iteration. When the count reaches ``capture_rate_warn_per_hour``
    # the guard emits a ``capture.rate_warn`` structlog warning so an
    # operator can investigate runaway capture (e.g. a stuck dedup
    # threshold, mis-tuned cadence, or a hostile foreground app). When
    # the count also reaches ``capture_rate_pause_per_hour`` AND that
    # threshold is non-zero, the iteration is skipped entirely with a
    # ``capture.rate_pause`` log line; the loop continues sleeping on
    # its normal cadence so it self-recovers as soon as the trailing
    # hour drops back below the threshold. Setting
    # ``capture_rate_pause_per_hour`` to 0 disables the pause arm of
    # the guard while still keeping the warning.
    capture_rate_warn_per_hour: int = Field(default=60, ge=0, le=1_000_000)
    capture_rate_pause_per_hour: int = Field(default=200, ge=0, le=1_000_000)

    # v1.11 — speech-only audio capture + Whisper transcription.
    # ``audio_retention_hot_days`` is the "hot" tier window for the
    # audio_segment table: rows older than this lose their on-disk
    # ``.wav`` / ``.opus`` payload (size_bytes := 0, path := "") but
    # keep their transcript forever — the lossless content track. A
    # random sample of segments equal to ``audio_keep_sample_pct``
    # bypasses the purge so a long-tail voice-signature corpus is
    # retained for future speaker-identification work.
    # ``audio_whisper_model`` selects the Whisper model size: ``"small"``
    # (~244 MB) hits the sweet spot between latency, RAM, and accuracy
    # on commodity desktop CPUs; ``"base"`` or ``"medium"`` are valid
    # overrides for users with more / less hardware budget.
    audio_retention_hot_days: int = Field(default=7, ge=1, le=365)
    audio_whisper_model: str = Field(default="small")
    audio_keep_sample_pct: float = Field(default=0.05, ge=0.0, le=1.0)

    # v1.11 feature 1/3 — speech-only audio capture worker. Hard-gated
    # by ``audio_capture_enabled`` (default False — opt-in for privacy,
    # same posture as the clipboard-history worker). The worker re-reads
    # the flag at startup but *not* on every iteration, so flipping it
    # requires a restart. ``audio_target_bitrate`` is the nominal
    # bits-per-second the encoder cascade aims for: Encodec is pinned
    # at 1500 bps in code, Opus narrowband sits at 4000 bps; this value
    # is stored on each ``audio_segment`` row so the dashboard can
    # render "opus @ 4 kbps" without re-deriving from the codec name.
    # ``audio_preferred_codec`` selects the first encoder the cascade
    # tries (one of ``"encodec"`` / ``"opus"`` / ``"opus_ffmpeg"``);
    # missing-backend cases fall through to the next candidate.
    # ``audio_vad_threshold`` is silero-vad's confidence cutoff in
    # ``[0.0, 1.0]`` — upstream default is 0.5, raise it to reject
    # background TV noise more aggressively, lower it for whispered
    # speech.
    audio_capture_enabled: bool = Field(default=False)
    # v1.13 — narrowband 8 kHz mono at 6 kbps is voice-call quality and
    # fits ~7 MB/day for 2-3 h of voiced speech (see STORAGE_BUDGET_DESIGN.md
    # §4). Encodec dropped from the default codec cascade because it costs
    # ~2.5 GB of torch deps and 88 MB of cached model weights for a feature
    # most users never opt into — opus_ffmpeg covers the budget bracket.
    audio_target_bitrate: int = Field(default=6000, ge=500, le=320_000)
    audio_preferred_codec: str = Field(default="opus")
    audio_vad_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    # v1.13 — VAD backend selector. "webrtcvad" is pure C (~4 KB, no
    # torch); "silero" stays for users who already have torch installed.
    # The audio worker reads this at startup; missing-backend cases fall
    # through to silero.
    audio_vad_backend: str = Field(default="webrtcvad")
    # v1.13 — Whisper transcription is now opt-in. Saves the 244 MB model
    # download on machines that just want voice memory without text.
    audio_transcribe_enabled: bool = Field(default=False)

    @model_validator(mode="after")
    def _validate_adaptive_bounds(self) -> Settings:
        if self.adaptive_max_seconds < self.adaptive_min_seconds:
            msg = (
                f"adaptive_max_seconds ({self.adaptive_max_seconds}) must be "
                f">= adaptive_min_seconds ({self.adaptive_min_seconds})"
            )
            raise ValueError(msg)
        return self

    @field_validator(
        "data_dir",
        "db_path",
        "thumbnails_dir",
        "inbox_path",
        "auto_backup_path",
        mode="after",
    )
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("tesseract_path", mode="before")
    @classmethod
    def _resolve_optional_path(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        if isinstance(value, Path) and str(value).strip() in ("", "."):
            return None
        return value

    @field_validator("log_level", mode="after")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            msg = f"Invalid log level: {value}"
            raise ValueError(msg)
        return upper

    def ensure_directories(self) -> None:
        """Create data directories if they do not yet exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.inbox_enabled:
            self.inbox_path.mkdir(parents=True, exist_ok=True)
        if self.auto_backup_enabled:
            self.auto_backup_path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    settings = Settings()
    settings.ensure_directories()
    return settings
