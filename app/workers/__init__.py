"""Background workers — capture loop, OCR worker, retention, embeddings, inbox."""

from app.workers.audio_retention_worker import run_audio_retention_worker
from app.workers.audio_worker import run_audio_worker
from app.workers.auto_backup_scheduler import run_auto_backup_scheduler
from app.workers.capture_loop import run_capture_loop
from app.workers.clipboard_worker import run_clipboard_worker
from app.workers.control import CaptureController, get_controller
from app.workers.daily_email_scheduler import run_daily_email_scheduler
from app.workers.day_end_summary_scheduler import run_day_end_summary_scheduler
from app.workers.digest_scheduler import run_digest_scheduler
from app.workers.embeddings_worker import run_embeddings_worker
from app.workers.inbox_worker import run_inbox_worker
from app.workers.monthly_digest_scheduler import run_monthly_digest_scheduler
from app.workers.ocr_worker import run_ocr_worker
from app.workers.retention import run_retention_worker
from app.workers.saved_search_alert import run_saved_search_alert_worker
from app.workers.webhook_retry_worker import run_webhook_retry_worker
from app.workers.weekly_digest_scheduler import run_weekly_digest_scheduler
from app.workers.weekly_stats_email_scheduler import (
    run_weekly_stats_email_scheduler,
)

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
