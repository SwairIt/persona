"""Explicit tool-access policy: owner keeps every tool, groups stay read-only.

Replaces the deleted ``_owner_tools_needed`` keyword/length heuristic, which
used to silently mute every tool whenever the owner's message was short and
matched no trigger word (e.g. «najdi foto kota» -- 15 characters, no
keyword match, tools lost).
"""

from __future__ import annotations

from app.integrations.telegram.tool_policy import READ_ONLY_TOOLS, allowed_tools

_EXECUTION_CLASS_TOOLS = (
    "run_shell",
    "run_mac",
    "install_mcp",
    "install_skill",
    "write_file",
    "delete_path",
)


def test_owner_private_gets_every_tool() -> None:
    assert allowed_tools(is_owner=True, is_group=False) is None


def test_owner_group_gets_exactly_read_only_tools() -> None:
    assert allowed_tools(is_owner=True, is_group=True) == READ_ONLY_TOOLS


def test_non_owner_group_gets_exactly_read_only_tools() -> None:
    assert allowed_tools(is_owner=False, is_group=True) == READ_ONLY_TOOLS


def test_non_owner_private_gets_no_tools() -> None:
    # A non-owner private turn should never reach the model in the first
    # place, but the policy itself must not depend on that being true
    # elsewhere -- it must fail closed on its own.
    assert allowed_tools(is_owner=False, is_group=False) == frozenset()


def test_no_execution_class_tool_ever_appears_in_a_group_result() -> None:
    """A future edit that widens the read-only set must fail loudly here
    rather than silently reaching Djima, Oleg, or either AI agent sharing
    the owner's group.
    """
    for is_owner in (True, False):
        result = allowed_tools(is_owner=is_owner, is_group=True)
        assert result is not None
        for name in _EXECUTION_CLASS_TOOLS:
            assert name not in result
