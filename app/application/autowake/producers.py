"""Safe, deterministic producer helpers for completed proactive artifacts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from app.application.autowake.service import EnqueueAutowake
from app.domains.autowake import DeliveryTarget, DeliveryTargetKind, SourceScope

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


async def enqueue_completed_research(
    service: AutowakeService,
    *,
    owner_user_id: int,
    chain_id: int,
    topic: str,
    conclusion: str,
    completed_at: datetime,
    source_scope: SourceScope,
    chat_id: int | None,
) -> EnqueueResult:
    """Deliver a concluded ``research`` chain back to the chat that asked.

    The whole point of research-on-request is that it answers where it was
    asked: a group-sourced request must land back in that SAME group, never
    in the owner's private DM/diary as if it were owner-private data. A
    non-group source (the owner asked in his own DM, or this was seeded some
    other owner-private way) falls back to the owner DM.
    """
    clean_topic = topic.strip()
    clean_conclusion = conclusion.strip()
    if not clean_topic or not clean_conclusion:
        raise ValueError("research topic and conclusion are required")
    text = (
        f"Почитала про «{clean_topic}» — вот что вынесла:\n\n"
        f"{clean_conclusion[:_MAX_PRODUCER_BODY_CHARS]}"
    )
    idempotency_key = f"research:{chain_id}"

    if source_scope is SourceScope.GROUP:
        if chat_id is None or chat_id >= 0:
            raise ValueError("group research delivery requires a negative Telegram chat id")
        return await service.enqueue(
            EnqueueAutowake(
                owner_user_id=owner_user_id,
                is_owner=True,
                kind="research.completed",
                source="telegram_group",
                source_scope=SourceScope.GROUP,
                text=text,
                idempotency_key=idempotency_key,
                target=DeliveryTarget(DeliveryTargetKind.GROUP, chat_id),
                # The chain's chat_id was recorded only for a chat that had
                # already asked Persona directly -- the same trust boundary
                # the request itself crossed, not a new opt-in invented here.
                group_opt_in_verified=True,
            ),
            now=completed_at,
        )

    return await service.enqueue(
        EnqueueAutowake(
            owner_user_id=owner_user_id,
            is_owner=True,
            kind="research.completed",
            source="persona_impulse",
            source_scope=SourceScope.DERIVED_OWNER,
            text=text,
            idempotency_key=idempotency_key,
        ),
        now=completed_at,
    )


__all__ = [
    "enqueue_completed_briefing",
    "enqueue_completed_dream_report",
    "enqueue_completed_research",
]
