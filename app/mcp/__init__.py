"""T24 (2026-06-08) — MCP (Model Context Protocol) infrastructure.

Phase 2 (2026-06-16) — the runtime stub became real. Alongside the
*built-in* tools and the DB registry of server configs, ``app.mcp.runtime``
now launches configured stdio MCP servers (JSON-RPC 2.0), discovers their
tools as ``mcp__{server}__{tool}`` and routes calls to them. The chat loop
keeps using the same ``<tool>name({...})</tool>`` syntax — ``call_tool``
fans out to builtins, the per-session browser agent, or external MCP
servers transparently.

Layers:
  * built-in tools (no Node/npm) — ``builtin_tools``
  * per-session interactive browser agent — ``app.browse.agent``
  * external stdio MCP servers — ``app.mcp.runtime`` (kv
    ``mcp_runtime_enabled``)
  * a DB table (``mcp_server``) of configured servers + ``/admin/mcp``
  * a per-feature switch ``/settings/automation`` (kv ``browser_backend``)
"""

from app.mcp.builtin_tools import (
    build_tools_prompt,
    builtin_command_to_tool_name,
    call_tool,
    get_builtin_tool,
    list_builtin_tools,
    parse_tool_calls,
)
from app.mcp.servers import (
    delete_server,
    get_server,
    list_servers,
    set_command,
    set_enabled,
    upsert_server,
)

# Tool names that belong to the interactive browser agent (gated by the
# ``browser_backend`` kv: a value of ``mcp`` hides them in favour of an
# external Playwright MCP server).
_BROWSER_AGENT_TOOL_NAMES = frozenset({
    "browser_open", "browser_click", "browser_type",
    "browser_read", "browser_screenshot", "browser_close",
})


async def get_browser_backend() -> str:
    """Which browser backend the user picked.

    ``remote`` keeps the built-in browser tool names but executes them on the
    owner's outbound-only Playwright PC worker.
    """
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415

    async with get_connection() as conn:
        raw = (await get_kv(conn, "browser_backend") or "builtin").strip().lower()
    return raw if raw in ("builtin", "remote", "mcp", "both") else "builtin"


async def enabled_builtin_tool_names() -> list[str]:
    """Return names of currently-enabled built-in tools (only those whose
    ``mcp_server.command`` starts with ``builtin:`` AND ``enabled=1``).

    Phase 2 — the interactive browser-agent tools are additionally gated by
    the ``browser_backend`` setting: when it is ``mcp`` we suppress them so
    the model uses an external Playwright MCP server instead of the built-in
    worker.
    """
    rows = await list_servers()
    names: list[str] = []
    for r in rows:
        if not r.get("enabled"):
            continue
        tool_name = builtin_command_to_tool_name(str(r.get("command", "")))
        if tool_name and get_builtin_tool(tool_name):
            names.append(tool_name)
    backend = await get_browser_backend()
    if backend == "mcp":
        names = [n for n in names if n not in _BROWSER_AGENT_TOOL_NAMES]
    return names


async def all_enabled_tool_names() -> list[str]:
    """Builtin (+ browser-agent, backend-gated) names plus discovered MCP
    tools. This is what the chat prompt advertises so the model can call
    external MCP tools by their ``mcp__server__tool`` name."""
    names = await enabled_builtin_tool_names()
    backend = await get_browser_backend()
    if backend in ("mcp", "both"):
        try:
            from app.mcp.runtime import discovered_mcp_tools  # noqa: PLC0415

            names = names + await discovered_mcp_tools()
        except Exception:  # noqa: BLE001 — discovery never blocks the prompt
            pass
    return names


__all__ = [
    "all_enabled_tool_names",
    "build_tools_prompt",
    "builtin_command_to_tool_name",
    "call_tool",
    "delete_server",
    "enabled_builtin_tool_names",
    "get_browser_backend",
    "get_server",
    "list_builtin_tools",
    "list_servers",
    "parse_tool_calls",
    "set_command",
    "set_enabled",
    "upsert_server",
]
