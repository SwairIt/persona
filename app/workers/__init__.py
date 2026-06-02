"""Background workers — capture loop, OCR worker, retention, embeddings."""

from app.workers.capture_loop import run_capture_loop
from app.workers.control import CaptureController, get_controller
from app.workers.digest_scheduler import run_digest_scheduler
from app.workers.embeddings_worker import run_embeddings_worker
from app.workers.ocr_worker import run_ocr_worker
from app.workers.retention import run_retention_worker
from app.workers.weekly_digest_scheduler import run_weekly_digest_scheduler

__all__ = [
    "CaptureController",
    "get_controller",
    "run_capture_loop",
    "run_digest_scheduler",
    "run_embeddings_worker",
    "run_ocr_worker",
    "run_retention_worker",
    "run_weekly_digest_scheduler",
]
