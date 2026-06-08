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

from app.mcp.servers import (
    delete_server,
    get_server,
    list_servers,
    set_command,
    set_enabled,
    upsert_server,
)

__all__ = [
    "delete_server",
    "get_server",
    "list_servers",
    "set_command",
    "set_enabled",
    "upsert_server",
]
