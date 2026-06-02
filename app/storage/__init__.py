"""Storage layer — SQLite + filesystem thumbnails."""

from app.storage.db import get_connection, init_database
from app.storage.models import CaptureEvent, DedupGroup, Screenshot

__all__ = [
    "CaptureEvent",
    "DedupGroup",
    "Screenshot",
    "get_connection",
    "init_database",
]
