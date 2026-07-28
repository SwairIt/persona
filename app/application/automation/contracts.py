"""Transport-neutral contracts for remote browser execution.

The server never forwards an arbitrary command to the owner's PC.  A request
must first become a :class:`BrowserAction`, whose constructor applies an exact
action/field allowlist and conservative size limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal
from urllib.parse import urlsplit

BrowserActionName = Literal[
    "open",
    "click",
    "type",
    "read",
    "screenshot",
    "close",
    "ping",
]

ALLOWED_BROWSER_ACTIONS: Final[frozenset[str]] = frozenset(
    {"open", "click", "type", "read", "screenshot", "close", "ping"}
)
_MAX_URL_CHARS = 2_048
_MAX_SELECTOR_CHARS = 1_024
_MAX_INPUT_CHARS = 16_384

_FIELDS: Final[dict[str, frozenset[str]]] = {
    "open": frozenset({"url"}),
    "click": frozenset({"selector"}),
    "type": frozenset({"selector", "text", "enter"}),
    "read": frozenset({"selector"}),
    "screenshot": frozenset({"full_page"}),
    "close": frozenset(),
    "ping": frozenset(),
}


class BrowserActionError(ValueError):
    """Untrusted browser action failed the strict schema."""


@dataclass(frozen=True, slots=True)
class BrowserAction:
    name: BrowserActionName
    arguments: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, name: str, raw: object) -> BrowserAction:  # noqa: PLR0912
        action = str(name or "").strip().lower()
        if action not in ALLOWED_BROWSER_ACTIONS:
            raise BrowserActionError(f"unsupported browser action: {action or '(empty)'}")
        if raw is None:
            args: dict[str, Any] = {}
        elif isinstance(raw, dict):
            args = dict(raw)
        else:
            raise BrowserActionError("browser action arguments must be an object")

        unknown = set(args) - _FIELDS[action]
        if unknown:
            raise BrowserActionError(
                f"unsupported fields for {action}: {', '.join(sorted(unknown))}"
            )

        clean: dict[str, Any]
        if action == "open":
            url = _bounded_string(args.get("url"), "url", _MAX_URL_CHARS)
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise BrowserActionError("url must be an absolute http(s) URL")
            if parsed.username is not None or parsed.password is not None:
                raise BrowserActionError("credentials in URL are not allowed")
            clean = {"url": url}
        elif action == "click":
            clean = {
                "selector": _bounded_string(
                    args.get("selector"), "selector", _MAX_SELECTOR_CHARS
                )
            }
        elif action == "type":
            clean = {
                "selector": _bounded_string(
                    args.get("selector"), "selector", _MAX_SELECTOR_CHARS
                ),
                "text": _bounded_string(
                    args.get("text"), "text", _MAX_INPUT_CHARS, allow_empty=True
                ),
                "enter": _strict_bool(args.get("enter", False), "enter"),
            }
        elif action == "read":
            clean = {
                "selector": _bounded_string(
                    args.get("selector", ""),
                    "selector",
                    _MAX_SELECTOR_CHARS,
                    allow_empty=True,
                )
            }
        elif action == "screenshot":
            clean = {
                "full_page": _strict_bool(args.get("full_page", True), "full_page")
            }
        else:
            clean = {}
        return cls(name=action, arguments=clean)  # type: ignore[arg-type]

    def as_payload(self) -> dict[str, Any]:
        return {"action": self.name, "arguments": dict(self.arguments)}

    @property
    def readonly(self) -> bool:
        return self.name in {"read", "screenshot", "ping"}


@dataclass(frozen=True, slots=True)
class BrowserCommand:
    owner_user_id: int
    session_id: int
    action: BrowserAction
    correlation_id: str
    is_owner: bool

    def __post_init__(self) -> None:
        if not self.is_owner:
            raise PermissionError("remote browser tools require the owner")
        if self.owner_user_id <= 0 or self.session_id <= 0:
            raise BrowserActionError("owner_user_id and session_id must be positive")
        if not self.correlation_id or len(self.correlation_id) > 128:
            raise BrowserActionError("correlation_id must contain 1..128 characters")


@dataclass(frozen=True, slots=True)
class BrowserJob:
    id: int
    owner_user_id: int
    session_id: int
    action: BrowserAction
    status: str
    worker_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    profile_key: str = ""
    resume_url: str | None = None


def immutable_result(value: dict[str, Any] | None) -> MappingProxyType[str, Any] | None:
    """Return a shallow immutable view for callers that need one."""
    return MappingProxyType(value) if value is not None else None


def _bounded_string(
    value: object,
    field_name: str,
    limit: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise BrowserActionError(f"{field_name} must be a string")
    if not allow_empty and not value:
        raise BrowserActionError(f"{field_name} is required")
    if len(value) > limit:
        raise BrowserActionError(f"{field_name} exceeds {limit} characters")
    if "\x00" in value:
        raise BrowserActionError(f"{field_name} contains a NUL byte")
    return value


def _strict_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise BrowserActionError(f"{field_name} must be a boolean")
    return value


__all__ = [
    "ALLOWED_BROWSER_ACTIONS",
    "BrowserAction",
    "BrowserActionError",
    "BrowserActionName",
    "BrowserCommand",
    "BrowserJob",
]
