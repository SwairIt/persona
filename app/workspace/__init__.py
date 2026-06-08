"""T27 (2026-06-08) — per-user file workspace on the server.

Each user has a private directory at ``data/workspaces/{user_id}/``
where the AI can read and write files via built-in tools. Files
written here are visible from any device the user signs in from
(via /workspace browse + download), and stay forever on the server
unless explicitly cleared.

Why on server, not on user's PC:
  * Multi-device — same files visible from phone, laptop, work PC.
  * The Persona web app runs server-side; tools call into Python here.
  * Avoids the security mess of letting AI touch arbitrary paths on
    the box where the user happens to be reading.

Public surface:
  * :func:`ensure_user_workspace(user_id)` — returns Path, creates
    directory on first call.
  * :func:`resolve_user_path(user_id, raw)` — given an LLM-provided
    string like ``"app.py"`` or ``"src/main.py"``, returns the
    absolute path inside the user's workspace. Refuses ``../`` and
    absolute paths outside the workspace.
  * :func:`list_user_files(user_id)` — recursive listing for the
    /workspace browse page.
"""

from app.workspace.dirs import (
    WorkspaceEscape,
    ensure_user_workspace,
    list_user_files,
    resolve_user_path,
    workspace_root,
)

__all__ = [
    "WorkspaceEscape",
    "ensure_user_workspace",
    "list_user_files",
    "resolve_user_path",
    "workspace_root",
]
