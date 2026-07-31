"""Read-only tool allowlist for a ``research`` thinking chain.

Mirrors the shape of ``app.integrations.telegram.tool_policy`` — an
explicit ``frozenset`` of allowed tool names, not a heuristic — rather than
inventing a parallel access-control mechanism (see that module's docstring
for the same reasoning).

A research chain runs unattended between steps of the thinking loop, with
no owner watching a single turn to catch a bad call, so it is narrower than
even the Telegram group allowlist: ``web_search`` and ``web_browse`` only.
Explicitly excluded:

* ``fetch_json`` — accepts POST/PUT/PATCH with arbitrary headers/body (see
  ``app/integrations/telegram/tool_policy.py`` for the identical reasoning
  in the group context).
* ``run_shell`` / ``run_mac`` / ``write_file`` / ``delete_path`` /
  ``install_mcp`` / ``install_skill`` — anything that writes or executes.

This module has no dependency on ``app.mcp`` or the Telegram integration —
just the two literal names — so ``app.thinking`` stays free of a write or
execute path, per ``tests/test_thinking_no_memory_writes.py``.
"""

from __future__ import annotations

#: The only tools a research chain may call, between steps, ever.
RESEARCH_TOOLS: frozenset[str] = frozenset({"web_search", "web_browse"})


def is_research_tool_allowed(name: str) -> bool:
    """Whether ``name`` may be called from inside a research chain."""
    return name in RESEARCH_TOOLS


__all__ = ["RESEARCH_TOOLS", "is_research_tool_allowed"]
