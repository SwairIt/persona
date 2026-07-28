"""Pure policy and value objects for proactive owner messages.

Autowake is deliberately fail-closed: only explicitly classified owner data
may become an outbound message.  Group/external context and likely secrets are
rejected before any message body is persisted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Final, Literal

MAX_AUTOWAKE_TEXT_CHARS: Final = 3_500
MAX_IDEMPOTENCY_KEY_CHARS: Final = 160


class SourceScope(StrEnum):
    """Privacy provenance attached by the producer."""

    OWNER_DIRECT = "owner_direct"
    OWNER_PRIVATE = "owner_private"
    DERIVED_OWNER = "derived_owner"
    GROUP = "group"
    EXTERNAL = "external"
    SECRET = "secret"  # noqa: S105 - privacy classification, not a credential


SAFE_SOURCE_SCOPES: Final[frozenset[SourceScope]] = frozenset(
    {
        SourceScope.OWNER_DIRECT,
        SourceScope.OWNER_PRIVATE,
        SourceScope.DERIVED_OWNER,
    }
)
SAFE_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "briefing",
        "calendar",
        "dream",
        "memory",
        "reminder",
        "system",
    }
)

_KIND_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IDEMPOTENCY_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.I),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
        r"bot[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S{8,}",
        re.I,
    ),
)
_GROUP_MARKERS: Final[tuple[str, ...]] = (
    "[telegram ·",
    "[telegram group",
    "chat_type=group",
    "chat_type=supergroup",
)

DeliveryDecisionKind = Literal["allow", "defer", "reject"]


@dataclass(frozen=True, slots=True)
class AutowakePolicyConfig:
    """Owner anti-spam defaults; all durations are explicit and bounded."""

    cooldown: timedelta = timedelta(hours=2)
    daily_cap: int = 4
    quiet_start_hour: int = 23
    quiet_end_hour: int = 8
    quiet_recheck: timedelta = timedelta(minutes=15)
    retry_base: timedelta = timedelta(seconds=30)
    retry_max: timedelta = timedelta(hours=2)
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if self.cooldown < timedelta(0):
            raise ValueError("cooldown cannot be negative")
        if not 1 <= self.daily_cap <= 24:
            raise ValueError("daily_cap must be in 1..24")
        if not 0 <= self.quiet_start_hour <= 23:
            raise ValueError("quiet_start_hour must be in 0..23")
        if not 0 <= self.quiet_end_hour <= 23:
            raise ValueError("quiet_end_hour must be in 0..23")
        if self.quiet_start_hour == self.quiet_end_hour:
            raise ValueError("quiet hours cannot cover the entire day")
        if self.quiet_recheck <= timedelta(0):
            raise ValueError("quiet_recheck must be positive")
        if self.retry_base <= timedelta(0) or self.retry_max < self.retry_base:
            raise ValueError("retry bounds are invalid")
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be in 1..20")


@dataclass(frozen=True, slots=True)
class DeliveryState:
    """Delivery history known at one policy evaluation."""

    delivered_today: int = 0
    last_delivered_at: datetime | None = None
    quiet_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    kind: DeliveryDecisionKind
    reason: str
    due_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProactiveContent:
    """Validated outbound content and its provenance."""

    kind: str
    source: str
    source_scope: SourceScope
    text: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not _KIND_PATTERN.fullmatch(self.kind):
            raise ValueError("kind must be a lowercase machine identifier")
        if self.source not in SAFE_SOURCES:
            raise ValueError("source is not allowlisted for autowake")
        if not _IDEMPOTENCY_PATTERN.fullmatch(self.idempotency_key):
            raise ValueError("invalid autowake idempotency key")
        clean = self.text.strip()
        if not clean:
            raise ValueError("autowake text cannot be empty")
        if len(clean) > MAX_AUTOWAKE_TEXT_CHARS:
            raise ValueError(f"autowake text exceeds {MAX_AUTOWAKE_TEXT_CHARS} characters")
        if "\x00" in clean:
            raise ValueError("autowake text contains a NUL byte")
        object.__setattr__(self, "text", clean)


def content_rejection_reason(content: ProactiveContent) -> str | None:
    """Return a stable rejection code without ever returning the message body."""

    if content.source_scope not in SAFE_SOURCE_SCOPES:
        return f"unsafe_source_scope:{content.source_scope.value}"
    lowered = content.text.casefold()
    if any(marker in lowered for marker in _GROUP_MARKERS):
        return "group_context_marker"
    if any(pattern.search(content.text) for pattern in _SECRET_PATTERNS):
        return "secret_like_content"
    return None


class AutowakePolicy:
    """Decide whether an already-safe owner message may be sent now."""

    def __init__(self, config: AutowakePolicyConfig | None = None) -> None:
        self.config = config or AutowakePolicyConfig()

    def evaluate(
        self,
        *,
        now: datetime,
        state: DeliveryState,
    ) -> DeliveryDecision:
        _require_aware(now)
        configured_quiet_until = state.quiet_until
        if configured_quiet_until is not None:
            _require_aware(configured_quiet_until)
        default_quiet = self._inside_default_quiet_hours(now)
        if (configured_quiet_until is not None and configured_quiet_until > now) or default_quiet:
            quiet_until = configured_quiet_until if configured_quiet_until is not None else now
            if default_quiet:
                quiet_until = max(quiet_until, self._default_quiet_end(now))
            return DeliveryDecision(
                kind="defer",
                reason="quiet_hours",
                due_at=max(
                    now + self.config.quiet_recheck,
                    quiet_until,
                ),
            )

        if state.delivered_today >= self.config.daily_cap:
            tomorrow = now.date() + timedelta(days=1)
            return DeliveryDecision(
                kind="defer",
                reason="daily_cap",
                due_at=datetime.combine(
                    tomorrow,
                    time(hour=self.config.quiet_end_hour),
                    tzinfo=now.tzinfo,
                ),
            )

        last = state.last_delivered_at
        if last is not None:
            _require_aware(last)
            cooldown_end = last.astimezone(now.tzinfo) + self.config.cooldown
            if cooldown_end > now:
                return DeliveryDecision(
                    kind="defer",
                    reason="cooldown",
                    due_at=cooldown_end,
                )

        return DeliveryDecision(kind="allow", reason="allowed", due_at=now)

    def retry_at(self, *, now: datetime, attempt: int) -> datetime:
        _require_aware(now)
        exponent = max(0, attempt - 1)
        seconds = self.config.retry_base.total_seconds() * (2**exponent)
        bounded = min(seconds, self.config.retry_max.total_seconds())
        return now + timedelta(seconds=bounded)

    def _inside_default_quiet_hours(self, now: datetime) -> bool:
        hour = now.hour
        start = self.config.quiet_start_hour
        end = self.config.quiet_end_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _default_quiet_end(self, now: datetime) -> datetime:
        end = now.replace(
            hour=self.config.quiet_end_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if now.hour >= self.config.quiet_start_hour or end <= now:
            end += timedelta(days=1)
        return end


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("autowake datetimes must be timezone-aware")


__all__ = [
    "MAX_AUTOWAKE_TEXT_CHARS",
    "SAFE_SOURCES",
    "SAFE_SOURCE_SCOPES",
    "AutowakePolicy",
    "AutowakePolicyConfig",
    "DeliveryDecision",
    "DeliveryState",
    "ProactiveContent",
    "SourceScope",
    "content_rejection_reason",
]
