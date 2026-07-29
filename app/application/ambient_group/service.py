"""Bounded decision orchestration for ambient Telegram groups."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.application.ambient_group.dto import (
    AmbientGroupOutcome,
    AmbientGroupTurn,
)

if TYPE_CHECKING:
    from app.application.ambient_group.ports import (
        AmbientGroupDecisionPort,
        AmbientGroupTurnPort,
        MonotonicClock,
    )


@dataclass(frozen=True, slots=True)
class _SystemClock:
    def now(self) -> float:
        return time.monotonic()


class AmbientGroupService:
    """Persist every message and selectively generate one bounded group reply."""

    def __init__(
        self,
        decision: AmbientGroupDecisionPort,
        turns: AmbientGroupTurnPort,
        *,
        clock: MonotonicClock | None = None,
        decision_timeout_seconds: float = 8.0,
        reply_timeout_seconds: float = 45.0,
        decision_interval_seconds: float = 2.0,
        reply_cooldown_seconds: float = 30.0,
    ) -> None:
        self._decision = decision
        self._turns = turns
        self._clock = clock or _SystemClock()
        self._decision_timeout = max(0.1, decision_timeout_seconds)
        self._reply_timeout = max(0.1, reply_timeout_seconds)
        self._decision_interval = max(0.0, decision_interval_seconds)
        self._reply_cooldown = max(0.0, reply_cooldown_seconds)
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_decision: dict[int, float] = {}
        self._last_reply: dict[int, float] = {}

    async def handle(self, turn: AmbientGroupTurn) -> AmbientGroupOutcome:
        chat_id = turn.external_chat_id
        async with self._locks[chat_id]:
            now = self._clock.now()
            rate_limited = self._within(
                now,
                self._last_reply.get(chat_id),
                self._reply_cooldown,
            ) or self._within(
                now,
                self._last_decision.get(chat_id),
                self._decision_interval,
            )
            if rate_limited:
                await self._turns.persist(turn)
                return AmbientGroupOutcome()
            self._last_decision[chat_id] = now
            try:
                async with asyncio.timeout(self._decision_timeout):
                    should_reply = await self._decision.should_reply(turn)
            except Exception:
                should_reply = False
            if not should_reply:
                await self._turns.persist(turn)
                return AmbientGroupOutcome()
            try:
                async with asyncio.timeout(self._reply_timeout):
                    answer = (await self._turns.reply(turn)).strip()
            except Exception:
                # reply() owns exactly-once user persistence before generation.
                answer = ""
            if answer:
                self._last_reply[chat_id] = self._clock.now()
            return AmbientGroupOutcome(answer)

    @staticmethod
    def _within(now: float, previous: float | None, window: float) -> bool:
        return previous is not None and now - previous < window


__all__ = ["AmbientGroupService"]
