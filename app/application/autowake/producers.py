"""Safe, deterministic producer helpers for completed proactive artifacts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from app.application.autowake.service import EnqueueAutowake
from app.domains.autowake import SourceScope

if TYPE_CHECKING:
    from datetime import datetime

    from app.application.autowake.ports import EnqueueResult
    from app.application.autowake.service import AutowakeService

_SLOT_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_PRODUCER_BODY_CHARS: Final = 2_800


async def enqueue_completed_briefing(
    service: AutowakeService,
    *,
    owner_user_id: int,
    slot: str,
    title: str,
    body: str,
    completed_at: datetime,
) -> EnqueueResult:
    """Queue one final owner briefing, never raw cards/transcripts/tool output."""
    clean_slot = slot.strip().lower()
    if not _SLOT_PATTERN.fullmatch(clean_slot):
        raise ValueError("invalid briefing slot")
    clean_title = title.strip()
    clean_body = body.strip()
    if not clean_title or not clean_body:
        raise ValueError("completed briefing title and body are required")
    text = f"{clean_title}\n\n{clean_body[:_MAX_PRODUCER_BODY_CHARS]}"
    return await service.enqueue(
        EnqueueAutowake(
            owner_user_id=owner_user_id,
            is_owner=True,
            kind="briefing.completed",
            source="briefing",
            source_scope=SourceScope.DERIVED_OWNER,
            text=text,
            idempotency_key=(
                f"briefing:{clean_slot}:{completed_at.date().isoformat()}"
            ),
        ),
        now=completed_at,
    )


async def enqueue_completed_dream_report(
    service: AutowakeService,
    *,
    owner_user_id: int,
    dream_run_id: int,
    report: str,
    completed_at: datetime,
    owner_private_only: bool,
) -> EnqueueResult:
    """Reusable dream hook; caller must prove the run excluded group sources."""
    if not owner_private_only:
        raise PermissionError("group-derived dream reports cannot enter autowake")
    if dream_run_id <= 0:
        raise ValueError("dream_run_id must be positive")
    clean = report.strip()
    if not clean:
        raise ValueError("completed dream report is empty")
    return await service.enqueue(
        EnqueueAutowake(
            owner_user_id=owner_user_id,
            is_owner=True,
            kind="dream.completed",
            source="dream",
            source_scope=SourceScope.DERIVED_OWNER,
            text=clean[:_MAX_PRODUCER_BODY_CHARS],
            idempotency_key=f"dream-report:{owner_user_id}:{dream_run_id}",
        ),
        now=completed_at,
    )


__all__ = ["enqueue_completed_briefing", "enqueue_completed_dream_report"]
