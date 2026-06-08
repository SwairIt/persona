"""T24 (2026-06-08) — MCP (Model Context Protocol) infrastructure stub.

Full implementation requires launching MCP servers as subprocesses and
mediating tool-use calls between the LLM and the servers — that's a
large engineering project (~1-2 weeks). This scaffolding gives:

  * A DB table (``mcp_server``) of configured servers
  * An admin page at /admin/mcp listing them with enable/disable
  * Default rows for common MCP servers (filesystem, shell, git, memory,
    brave-search) — disabled by default
  * Stub functions :func:`list_servers` / :func:`set_enabled` /
    :func:`set_command` so future code can integrate without touching
    the DB layer

When the actual MCP runtime ships, the route ``/api/chat/sessions/{id}/send-stream``
will:
  1. Read enabled servers
  2. Launch subprocesses with ``stdio_transport``
  3. Discover their tools
  4. Inject tool descriptions into the LLM prompt
  5. Parse model's tool-use calls
  6. Execute and feed results back
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


async def enabled_builtin_tool_names() -> list[str]:
    """Return names of currently-enabled built-in tools (only those whose
    ``mcp_server.command`` starts with ``builtin:`` AND ``enabled=1``)."""
    rows = await list_servers()
    names: list[str] = []
    for r in rows:
        if not r.get("enabled"):
            continue
        tool_name = builtin_command_to_tool_name(str(r.get("command", "")))
        if tool_name and get_builtin_tool(tool_name):
            names.append(tool_name)
    return names


__all__ = [
    "build_tools_prompt",
    "builtin_command_to_tool_name",
    "call_tool",
    "delete_server",
    "enabled_builtin_tool_names",
    "get_builtin_tool",
    "get_server",
    "list_builtin_tools",
    "list_servers",
    "parse_tool_calls",
    "set_command",
    "set_enabled",
    "upsert_server",
]
