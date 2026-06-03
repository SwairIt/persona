"""Pydantic models for storage rows. Plain dataclasses-equivalent — no ORM."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

OcrStatus = Literal["pending", "done", "skipped", "failed"]
CaptureEventType = Literal["start", "pause", "resume", "error", "heartbeat", "cleanup"]
Tier = Literal["hot", "warm", "cold", "pinned"]


class Screenshot(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    captured_at: datetime
    monitor_index: int
    width: int
    height: int
    thumbnail_path: str | None
    phash: str
    app_name: str | None
    window_title: str | None
    process_name: str | None
    ocr_status: OcrStatus
    ocr_text: str | None
    dedup_group_id: int | None
    created_at: datetime
    tier: Tier = "hot"
    is_private: bool = False
    # v0.70 — per-shot lock guard. Locked rows are filtered out of
    # :mod:`app.bulk_delete` and rejected by
    # :func:`app.recycle.soft_delete_screenshot`. Default ``False``
    # keeps every legacy row writable without a backfill.
    locked: bool = False


class DedupGroup(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    representative_screenshot_id: int | None
    phash: str
    seen_count: int
    first_seen: datetime
    last_seen: datetime


class CaptureEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    event_type: CaptureEventType
    details: str | None
