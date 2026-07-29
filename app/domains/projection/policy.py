"""Fail-closed eligibility rules for derived memory projections."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domains.projection.model import ProjectionSource

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:password|passwd|парол[ьи]|api[\s_-]?key|secret|секрет|token|токен|"
    r"bot[\s_-]?token|private[\s_-]?key)\s*(?::|=|is|это)\s*\S{4,}"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}")
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_PEM_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_KNOWN_TOKEN = re.compile(
    r"(?:"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bsk-[A-Za-z0-9_-]{20,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bAIza[0-9A-Za-z_-]{30,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"
    r")"
)
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_OPAQUE_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_~+/=.-]{32,160}(?![A-Za-z0-9])")
_MIN_TEXT_CHARS = 6


@dataclass(frozen=True, slots=True)
class ProjectionDecision:
    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ProjectionPolicy:
    max_text_chars: int = 600

    def decide(self, source: ProjectionSource) -> ProjectionDecision:
        text = " ".join(source.text.split())
        secret_material = "\n".join(
            (text, *(item.excerpt or "" for item in source.evidence))
        )
        rejected_reason = next(
            (
                reason
                for condition, reason in (
                    (source.owner_user_id <= 0, "invalid_owner"),
                    (source.candidate_status != "applied", "candidate_not_applied"),
                    (
                        source.revision_action not in {"add", "update"},
                        "revision_not_projectable",
                    ),
                    (not source.memory_active, "memory_inactive"),
                    (source.memory_pinned, "memory_pinned"),
                    (
                        not _MIN_TEXT_CHARS <= len(text) <= self.max_text_chars,
                        "text_length_out_of_bounds",
                    ),
                    (
                        any(
                            item.source_kind == "telegram_group"
                            for item in source.evidence
                        ),
                        "group_evidence",
                    ),
                    (
                        not any(
                            item.trusted_owner_chat for item in source.evidence
                        ),
                        "missing_trusted_owner_evidence",
                    ),
                    (_contains_secret(secret_material), "secret_material"),
                )
                if condition
            ),
            None,
        )
        if rejected_reason is not None:
            return ProjectionDecision(False, rejected_reason)
        return ProjectionDecision(True, "eligible")


def _contains_secret(text: str) -> bool:
    if any(
        pattern.search(text)
        for pattern in (
            _SECRET_ASSIGNMENT,
            _BEARER,
            _TELEGRAM_TOKEN,
            _PEM_KEY,
            _KNOWN_TOKEN,
            _JWT,
        )
    ):
        return True
    return any(
        _looks_like_opaque_secret(match.group(0))
        for match in _OPAQUE_CANDIDATE.finditer(text)
    )


def _looks_like_opaque_secret(value: str) -> bool:
    """Conservative entropy gate for unlabeled credentials.

    Natural words, UUIDs and hex commit hashes are intentionally ignored. A
    candidate must mix letter case, digits and a non-alphanumeric token
    character before entropy is considered.
    """

    categories = (
        any(char.islower() for char in value),
        any(char.isupper() for char in value),
        any(char.isdigit() for char in value),
        any(not char.isalnum() for char in value),
    )
    if not all(categories):
        return False
    counts = Counter(value)
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in counts.values()
    )
    return entropy >= 4.3


__all__ = ["ProjectionDecision", "ProjectionPolicy"]
