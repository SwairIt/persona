"""Background-worker public API with lazy compatibility exports.

Importing ``app.workers.control`` must not import capture, OCR, audio, email and
every scheduler. PEP 562 attribute loading preserves the legacy
``from app.workers import run_capture_loop`` API while deferring each concrete
module until that attribute is actually requested.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, tuple[str, str]] = {
    "CaptureController": ("app.workers.control", "CaptureController"),
    "get_controller": ("app.workers.control", "get_controller"),
    "run_audio_retention_worker": (
        "app.workers.audio_retention_worker",
        "run_audio_retention_worker",
    ),
    "run_audio_worker": ("app.workers.audio_worker", "run_audio_worker"),
    "run_auto_backup_scheduler": (
        "app.workers.auto_backup_scheduler",
        "run_auto_backup_scheduler",
    ),
    "run_capture_loop": ("app.workers.capture_loop", "run_capture_loop"),
    "run_clipboard_worker": ("app.workers.clipboard_worker", "run_clipboard_worker"),
    "run_daily_email_scheduler": (
        "app.workers.daily_email_scheduler",
        "run_daily_email_scheduler",
    ),
    "run_day_end_summary_scheduler": (
        "app.workers.day_end_summary_scheduler",
        "run_day_end_summary_scheduler",
    ),
    "run_digest_scheduler": ("app.workers.digest_scheduler", "run_digest_scheduler"),
    "run_embeddings_worker": (
        "app.workers.embeddings_worker",
        "run_embeddings_worker",
    ),
    "run_inbox_worker": ("app.workers.inbox_worker", "run_inbox_worker"),
    "run_monthly_digest_scheduler": (
        "app.workers.monthly_digest_scheduler",
        "run_monthly_digest_scheduler",
    ),
    "run_ocr_worker": ("app.workers.ocr_worker", "run_ocr_worker"),
    "run_retention_worker": ("app.workers.retention", "run_retention_worker"),
    "run_saved_search_alert_worker": (
        "app.workers.saved_search_alert",
        "run_saved_search_alert_worker",
    ),
    "run_webhook_retry_worker": (
        "app.workers.webhook_retry_worker",
        "run_webhook_retry_worker",
    ),
    "run_weekly_digest_scheduler": (
        "app.workers.weekly_digest_scheduler",
        "run_weekly_digest_scheduler",
    ),
    "run_weekly_stats_email_scheduler": (
        "app.workers.weekly_stats_email_scheduler",
        "run_weekly_stats_email_scheduler",
    ),
}

__all__ = [
    "CaptureController",
    "get_controller",
    "run_audio_retention_worker",
    "run_audio_worker",
    "run_auto_backup_scheduler",
    "run_capture_loop",
    "run_clipboard_worker",
    "run_daily_email_scheduler",
    "run_day_end_summary_scheduler",
    "run_digest_scheduler",
    "run_embeddings_worker",
    "run_inbox_worker",
    "run_monthly_digest_scheduler",
    "run_ocr_worker",
    "run_retention_worker",
    "run_saved_search_alert_worker",
    "run_webhook_retry_worker",
    "run_weekly_digest_scheduler",
    "run_weekly_stats_email_scheduler",
]


def __getattr__(name: str) -> object:
    """Resolve one legacy export and cache it on this package."""

    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg) from exc

    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})
