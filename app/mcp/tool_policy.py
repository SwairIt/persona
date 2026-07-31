"""Fail-closed risk classification for autonomous Persona tool turns."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    UNSAFE_NETWORK = "unsafe_network"
    MUTATING = "mutating"
    EXECUTION = "execution"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


_BUILTIN_RISKS: Final[dict[str, ToolRisk]] = {
    "read_file": ToolRisk.READ_ONLY,
    "list_dir": ToolRisk.READ_ONLY,
    "git_status": ToolRisk.READ_ONLY,
    # Direct Playwright navigation does not pass through the remote-browser
    # URL/SSRF policy, so "read-only" is not sufficient for autonomy.
    "web_browse": ToolRisk.UNSAFE_NETWORK,
    "read_many": ToolRisk.READ_ONLY,
    "find_files": ToolRisk.READ_ONLY,
    "search_code": ToolRisk.READ_ONLY,
    # The legacy helper accepts POST/PUT/PATCH, arbitrary headers/body and
    # redirects. Treat the whole tool as mutating until its wire contract is
    # split into a strict GET-only, per-hop-validated variant.
    "fetch_json": ToolRisk.MUTATING,
    "web_search": ToolRisk.READ_ONLY,
    "verify_media_url": ToolRisk.READ_ONLY,
    # Legacy recall updates access_count/last_seen and therefore future
    # salience. Keep model-driven rehearsal out of autonomous tool turns.
    "query_memory": ToolRisk.MUTATING,
    # The builtin backend validates the initial URL but not every redirect,
    # subrequest or DNS resolution. Reading an existing persistent page can
    # also expose a user-navigated private surface. Keep all browser session
    # access out of autonomous turns until every backend has the same
    # per-request SSRF guard as the remote PC worker.
    "browser_open": ToolRisk.UNSAFE_NETWORK,
    "browser_read": ToolRisk.UNSAFE_NETWORK,
    "browser_screenshot": ToolRisk.UNSAFE_NETWORK,
    "write_file": ToolRisk.MUTATING,
    "edit_file": ToolRisk.MUTATING,
    "multi_edit": ToolRisk.MUTATING,
    "schedule_reminder": ToolRisk.MUTATING,
    "browser_click": ToolRisk.MUTATING,
    "browser_type": ToolRisk.MUTATING,
    "browser_close": ToolRisk.MUTATING,
    "install_skill": ToolRisk.EXECUTION,
    "install_mcp": ToolRisk.EXECUTION,
    "run_shell": ToolRisk.EXECUTION,
    "run_mac": ToolRisk.EXECUTION,
    "run_tests": ToolRisk.EXECUTION,
    "delete_path": ToolRisk.DESTRUCTIVE,
}


def tool_risk(name: str) -> ToolRisk:
    """Return UNKNOWN for external or newly added tools until reviewed."""
    return _BUILTIN_RISKS.get(str(name), ToolRisk.UNKNOWN)


def autonomous_tool_names(names: Iterable[str]) -> frozenset[str]:
    """Allow only explicitly reviewed read-only tools for model autonomy."""
    return frozenset(name for name in names if tool_risk(name) is ToolRisk.READ_ONLY)


__all__ = ["ToolRisk", "autonomous_tool_names", "tool_risk"]
