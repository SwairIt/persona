"""SQLite adapter for one-use worker enrollment."""

from app.adapters.worker_enrollment.repository import SqliteWorkerEnrollment

__all__ = ["SqliteWorkerEnrollment"]
