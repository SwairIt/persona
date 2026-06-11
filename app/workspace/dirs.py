"""Per-user workspace directory helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger("persona.workspace")


class WorkspaceEscape(Exception):
    """Raised when an LLM-provided path tries to escape the user's
    workspace via ``../`` or absolute paths."""


def workspace_root() -> Path:
    """Base directory under which every user's workspace lives."""
    base = Path(get_settings().data_dir).expanduser() / "workspaces"
    base.mkdir(parents=True, exist_ok=True)
    return base


def ensure_user_workspace(user_id: int) -> Path:
    """Return + create the workspace directory for one user."""
    if user_id <= 0:
        # Anonymous / dev mode — single shared workspace under id 0.
        user_id = 0
    p = workspace_root() / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_user_path(user_id: int, raw: str) -> Path:
    """Map an LLM-provided path string to an absolute path INSIDE the
    user's workspace. Raises :class:`WorkspaceEscape` if the path tries
    to break out.

    Acceptable inputs:
      * ``"app.py"`` → ``data/workspaces/{uid}/app.py``
      * ``"src/main.py"`` → ``data/workspaces/{uid}/src/main.py``
      * absolute path UNDER workspace_root — accepted as-is
      * starts with ``"~"`` — refused (forces inside-workspace semantic)
    """
    root = ensure_user_workspace(user_id)
    raw = (raw or "").strip()
    if not raw:
        raise WorkspaceEscape("empty path")

    # Common AI behaviour: gives "D:\Projects\app.py" or "/home/user/foo".
    # If absolute path is OUTSIDE workspace_root → refuse. If INSIDE
    # (e.g. user copy-pasted from a successful list_dir) — accept.
    if os.path.isabs(raw):
        candidate = Path(raw).resolve()
    else:
        # Strip any leading slashes / backslashes that LLM might add.
        cleaned = raw.lstrip("/\\")
        # T29 — the model keeps prepending 'workspace/' even though paths are
        # already relative to the workspace ROOT (it created a junk nested
        # workspace/ folder). Strip a leading 'workspace/' so the intended
        # path resolves correctly instead of nesting.
        norm = cleaned.replace("\\", "/")
        if norm.lower() == "workspace":
            cleaned = ""
        elif norm.lower().startswith("workspace/"):
            cleaned = cleaned[len("workspace/"):]
        candidate = (root / cleaned).resolve()

    # Hard check: result must be inside the workspace root.
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise WorkspaceEscape(
            f"path escapes workspace: {raw} → {candidate}"
        ) from exc
    return candidate


def list_user_files(user_id: int) -> list[dict[str, Any]]:
    """Return recursive listing of user's workspace for the /workspace
    page. Each entry: ``{relative_path, size, modified_at, is_dir}``."""
    root = ensure_user_workspace(user_id)
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        try:
            rel = path.relative_to(root)
            stat = path.stat()
        except OSError:
            continue
        out.append({
            "relative_path": rel.as_posix(),
            "size": 0 if path.is_dir() else stat.st_size,
            "modified_at": int(stat.st_mtime),
            "is_dir": path.is_dir(),
        })
    return out
