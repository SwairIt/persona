"""Compatibility gateway from current browser tools to the remote service."""

from __future__ import annotations

import base64
import binascii
import uuid
from pathlib import Path
from typing import Any, Final

from app.adapters.remote_browser.repository import SqliteRemoteBrowserJobs
from app.application.automation.contracts import BrowserAction, BrowserCommand
from app.application.automation.service import RemoteBrowserService

_MAX_SCREENSHOT_BYTES: Final[int] = 1_500_000
_jobs = SqliteRemoteBrowserJobs()
_service = RemoteBrowserService(_jobs)


async def execute(
    user_id: int,
    session_id: int,
    command: str,
    **arguments: Any,
) -> dict[str, Any]:
    """Run a legacy manager-shaped command on the authenticated owner PC.

    ``path`` remains server-local and is never serialized into the remote job.
    """
    from app.auth.owner import is_owner  # noqa: PLC0415

    owner = await is_owner(int(user_id))
    remote_arguments = _remote_arguments(command, arguments)
    action = BrowserAction.parse(command, remote_arguments)
    result = await _service.execute(
        BrowserCommand(
            owner_user_id=int(user_id),
            session_id=int(session_id),
            action=action,
            correlation_id=f"browser-{session_id}-{uuid.uuid4().hex}",
            is_owner=owner,
        )
    )
    if command == "screenshot":
        await _persist_screenshot(user_id, arguments.get("path"), result)
    return result


def _remote_arguments(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if command == "open":
        return {"url": arguments.get("url")}
    if command == "click":
        return {"selector": arguments.get("selector")}
    if command == "type":
        return {
            "selector": arguments.get("selector"),
            "text": arguments.get("text"),
            "enter": bool(arguments.get("enter", False)),
        }
    if command == "read":
        return {"selector": arguments.get("selector", "")}
    if command == "screenshot":
        return {"full_page": bool(arguments.get("full_page", True))}
    return {}


async def _persist_screenshot(
    user_id: int,
    requested_path: object,
    result: dict[str, Any],
) -> None:
    from app.workspace import ensure_user_workspace  # noqa: PLC0415

    if not isinstance(requested_path, str) or not requested_path:
        raise ValueError("server screenshot path is required")
    workspace = ensure_user_workspace(int(user_id)).resolve()
    target = Path(requested_path).resolve()
    if workspace != target and workspace not in target.parents:
        raise ValueError("screenshot path escaped the owner workspace")
    encoded = result.pop("screenshot_base64", None)
    if not isinstance(encoded, str):
        raise ValueError("remote worker returned no screenshot")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("remote worker returned an invalid screenshot") from None
    if not raw or len(raw) > _MAX_SCREENSHOT_BYTES:
        raise ValueError("remote screenshot exceeded the local artifact limit")
    if result.get("mime_type") != "image/jpeg" or not raw.startswith(b"\xff\xd8\xff"):
        raise ValueError("remote screenshot is not a JPEG")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)


__all__ = ["execute"]
