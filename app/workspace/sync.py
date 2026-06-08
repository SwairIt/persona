"""T28 (2026-06-08) — workspace → device file sync.

When the AI writes a file via the ``write_file`` built-in tool, the file
lands in the canonical server workspace (``data/workspaces/{user_id}/``)
AND an append-only row is written to ``workspace_file_event``. The device
the user picked as their "code write target" (see
:func:`app.devices.set_code_write_target`) polls
``GET /api/workspace/sync`` and replays those events into a local
directory (``~/persona-workspace/`` on a Mac).

The event log only records *that* a path changed plus its byte size — the
authoritative content always lives in the workspace on disk. The sync
payload reads the current file content at pull time, so a file written
three times only ships its latest bytes once (events are deduped to the
latest op per path).
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.workspace.sync")

_VALID_OPS = ("write", "delete")


async def record_file_event(
    user_id: int,
    relative_path: str,
    operation: str = "write",
    content_bytes: int = 0,
) -> None:
    """Append a workspace change to the sync log.

    Best-effort by contract: callers wrap this so a logging failure never
    breaks the write that already succeeded on disk.
    """
    if operation not in _VALID_OPS:
        raise ValueError(f"operation must be one of {_VALID_OPS}, got {operation!r}")
    rel = (relative_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("relative_path required")
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO workspace_file_event "
            "  (user_id, relative_path, operation, content_bytes) "
            "VALUES (?, ?, ?, ?)",
            (user_id, rel, operation, max(0, int(content_bytes))),
        )
        await conn.commit()


async def list_file_events_since(
    user_id: int,
    since_id: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Raw event rows with ``id > since_id``, oldest first."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, relative_path, operation, content_bytes, created_at "
            "FROM workspace_file_event "
            "WHERE user_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (user_id, int(since_id), int(limit)),
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(r["id"]),
            "relative_path": str(r["relative_path"]),
            "operation": str(r["operation"]),
            "content_bytes": int(r["content_bytes"]),
            "created_at": str(r["created_at"]),
        }
        for r in rows
    ]


async def build_sync_payload(
    user_id: int,
    since_id: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    """Build the response for ``GET /api/workspace/sync``.

    Returns ``{"cursor": int, "files": [...]}`` where each file is
    ``{relative_path, operation, content}``. Events are deduped to the
    latest operation per path so a file edited N times in the window only
    ships once. For ``write`` ops the *current* file content is read from
    the workspace at pull time (the log stores only the byte count). A
    write whose file has since vanished from disk is downgraded to a
    ``delete`` so the device mirror stays consistent.

    ``cursor`` is the largest event id considered (including deduped-away
    rows) so the caller can advance its watermark past everything it has
    now seen, not just the rows that survived dedup.
    """
    # Lazy import to avoid a circular import at module load (dirs → settings
    # is fine, but keep the symmetry with the rest of the package).
    from app.workspace.dirs import WorkspaceEscape, resolve_user_path  # noqa: PLC0415

    events = await list_file_events_since(user_id, since_id=since_id, limit=limit)
    cursor = events[-1]["id"] if events else int(since_id)

    # Dedup: keep the LAST event per path (later ops supersede earlier).
    latest: dict[str, str] = {}
    order: list[str] = []
    for ev in events:
        path = ev["relative_path"]
        if path not in latest:
            order.append(path)
        latest[path] = ev["operation"]

    files: list[dict[str, Any]] = []
    for path in order:
        op = latest[path]
        if op == "delete":
            files.append({"relative_path": path, "operation": "delete", "content": None})
            continue
        # write — read current bytes off disk.
        try:
            resolved = resolve_user_path(user_id, path)
        except WorkspaceEscape:
            # A path that no longer resolves inside the workspace is junk;
            # tell the device to drop it.
            files.append({"relative_path": path, "operation": "delete", "content": None})
            continue
        if not resolved.exists() or resolved.is_dir():
            files.append({"relative_path": path, "operation": "delete", "content": None})
            continue
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("workspace_sync.read_failed", path=path, error=str(exc))
            continue
        files.append({"relative_path": path, "operation": "write", "content": content})

    return {"cursor": cursor, "files": files}


__all__ = [
    "build_sync_payload",
    "list_file_events_since",
    "record_file_event",
]
