"""Datetime <-> ISO 8601 string helpers shared by storage and search."""

from __future__ import annotations

from datetime import datetime, timezone


def iso(value: datetime) -> str:
    """Convert a naive or aware datetime to an ISO 8601 string (UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 string into a timezone-aware datetime."""
    return datetime.fromisoformat(value)
