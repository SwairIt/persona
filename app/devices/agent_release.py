"""T29 (2026-06-08) — agent release version + outdated-install detection.

The Mac agent reports its version in the User-Agent of every request
(``persona-agent/X.Y`` on ingest, ``persona-mac-agent/X.Y`` on the sync
/ heartbeat calls). The heartbeat UA lands in ``device.user_agent``, so
the server can compare a device's running agent against the latest
release and surface an "update available" banner on the main page.

Keep :data:`LATEST_AGENT_VERSION` in sync with ``AGENT_VERSION`` in
``mac-agent/persona_agent.py`` (and ``_AGENT_VERSION`` in
``mac-agent/sync_client.py``) — bump all three together on any release
that changes agent behaviour.
"""

from __future__ import annotations

import re
from typing import Any

from app.storage.db import get_connection

# Minimum agent version that works well — drives the "update available"
# banner. The real screenshot-upload breakage was server-side (auth gate +
# field name), fixed without an agent change, so 1.13 is fine and we DON'T
# nag those users. 1.14 (X-Agent-Token + opt-in audio) is an optional
# improvement new installs get; bump this only when an update is required.
LATEST_AGENT_VERSION = "1.13"

# Matches both ``persona-agent/1.13`` and ``persona-mac-agent/1.13``.
_VERSION_RE = re.compile(r"persona-(?:mac-)?agent/(\d+)\.(\d+)")


def _version_tuple(v: str) -> tuple[int, int]:
    parts = v.split(".")
    major = int(parts[0]) if parts and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return (major, minor)


def parse_agent_version(user_agent: str | None) -> tuple[int, int] | None:
    """Extract ``(major, minor)`` from a Persona-agent UA, or None."""
    if not user_agent:
        return None
    match = _VERSION_RE.search(user_agent)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _is_persona_agent_ua(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    ua = user_agent.lower()
    return "persona-agent" in ua or "persona-mac-agent" in ua


def is_outdated_ua(user_agent: str | None) -> bool:
    """True when the UA is our agent AND older than the latest release.

    A UA that is clearly our agent but carries no parseable version (a
    pre-versioning build) counts as outdated. A non-agent UA (e.g. the
    browser UA stored when a device is added by hand on /devices, before
    the agent ever ran) is NOT flagged — we never nag about a device that
    hasn't run the agent yet.
    """
    if not _is_persona_agent_ua(user_agent):
        return False
    parsed = parse_agent_version(user_agent)
    if parsed is None:
        return True
    return parsed < _version_tuple(LATEST_AGENT_VERSION)


async def outdated_agent(user_id: int) -> dict[str, Any] | None:
    """Return info about the user's most-recently-seen outdated agent
    device, or None when every agent is current (or none has run yet).

    Shape: ``{device_id, name, kind, current_version, latest_version}``.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, kind, user_agent FROM device "
            "WHERE user_id = ? AND user_agent IS NOT NULL "
            "ORDER BY COALESCE(last_seen_at, created_at) DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
    for row in rows:
        ua = str(row["user_agent"])
        if is_outdated_ua(ua):
            parsed = parse_agent_version(ua)
            return {
                "device_id": int(row["id"]),
                "name": str(row["name"]),
                "kind": str(row["kind"]),
                "current_version": f"{parsed[0]}.{parsed[1]}" if parsed else "?",
                "latest_version": LATEST_AGENT_VERSION,
            }
    return None


async def mac_agent_update_prompt(user_id: int) -> dict[str, Any] | None:
    """Decide whether to show the Mac-agent update/setup banner.

    Two cases, in priority order:

    1. ``outdated`` — the user has a *device* running our agent on an older
       version. Precise: we know the version from ``device.user_agent``.
    2. ``setup`` — the instance has a live (non-revoked) Mac ingest agent
       (a ``remote_agent`` row) but the user has no device on the current
       version. This is the bootstrap case: the agent was installed the old
       ingest-only way, so it never registered a ``device`` (no version, no
       T28 sync) — a reinstall via the new installer fixes that.

    ``remote_agent`` rows are not user-scoped, so case 2 is evaluated
    instance-wide. That's correct for the single-user deployments Persona
    targets, and otherwise harmless — the banner only ever links the user
    to the install page.

    Returns None when a current-version device already exists.
    """
    latest = _version_tuple(LATEST_AGENT_VERSION)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name, user_agent FROM device "
            "WHERE user_id = ? AND user_agent IS NOT NULL "
            "ORDER BY COALESCE(last_seen_at, created_at) DESC",
            (user_id,),
        )
        devices = await cursor.fetchall()
        # A device already on the latest version → nothing to nag about.
        if any(parse_agent_version(d["user_agent"]) == latest for d in devices):
            return None
        # Precise: a device running an older build.
        for d in devices:
            if is_outdated_ua(d["user_agent"]):
                parsed = parse_agent_version(d["user_agent"])
                return {
                    "name": str(d["name"]),
                    "current_version": f"{parsed[0]}.{parsed[1]}" if parsed else "?",
                    "latest_version": LATEST_AGENT_VERSION,
                    "reason": "outdated",
                }
        # Bootstrap: a live Mac ingest agent exists but no versioned device.
        cursor = await conn.execute(
            "SELECT 1 FROM remote_agent "
            "WHERE revoked_at IS NULL "
            "AND LOWER(COALESCE(platform, '')) LIKE 'mac%' LIMIT 1"
        )
        if await cursor.fetchone():
            return {
                "name": "Mac",
                "current_version": "—",
                "latest_version": LATEST_AGENT_VERSION,
                "reason": "setup",
            }
    return None


__all__ = [
    "LATEST_AGENT_VERSION",
    "is_outdated_ua",
    "mac_agent_update_prompt",
    "outdated_agent",
    "parse_agent_version",
]
