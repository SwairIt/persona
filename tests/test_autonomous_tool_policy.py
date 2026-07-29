"""Autonomous model turns must never inherit dangerous enabled tools."""

from __future__ import annotations

from app.mcp.builtin_tools import list_builtin_tools
from app.mcp.tool_policy import ToolRisk, autonomous_tool_names, tool_risk


def test_every_builtin_tool_has_an_explicit_risk_classification() -> None:
    unclassified = [
        name
        for name in list_builtin_tools()
        if tool_risk(name) is ToolRisk.UNKNOWN
    ]

    assert unclassified == []


def test_autonomous_allowlist_excludes_side_effects_and_external_tools() -> None:
    enabled = [
        *list_builtin_tools(),
        "mcp__playwright__click",
        "mcp__filesystem__read-file",
    ]

    allowed = autonomous_tool_names(enabled)

    assert {"read_file", "search_code", "web_search"} <= allowed
    assert {
        "delete_path",
        "run_mac",
        "install_mcp",
        "write_file",
        "schedule_reminder",
        "browser_click",
        "browser_type",
        "browser_open",
        "browser_read",
        "browser_screenshot",
        "fetch_json",
        "web_browse",
        "query_memory",
        "mcp__playwright__click",
        "mcp__filesystem__read-file",
    }.isdisjoint(allowed)
