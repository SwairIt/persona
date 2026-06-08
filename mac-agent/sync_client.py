"""Sync client for the Mac agent.

Drop-in helper the agent can call to:
  * heartbeat the server every N seconds (so /devices shows last_seen),
  * push local events to /api/sync/push,
  * pull remote events from /api/sync/pull and hand them to a callback.

The agent's existing audio + screen loops already authenticate to the
ingest API with ``server.token`` (the OG agent token from ``config.py``).
This client adds a SECOND credential — the per-device ``device_token``
the user generated at /devices — used only for the sync endpoints. The
two are kept separate because their scopes are different: the OG token
authenticates ``/api/agent/*`` (legacy ingest), the device token
authenticates ``/api/sync/*`` (multi-device).

Usage from the agent main loop::

    client = SyncClient(server_url, device_token)
    await client.heartbeat()

    # After locally transcribing audio into a Note row:
    await client.push_events([
        {
            "kind": "note",
            "op": "insert",
            "payload": {"uuid": note_uuid, "body": text, "title": title},
            "logical_clock": int(time.time() * 1000),
        }
    ])

    # Periodically:
    events = await client.pull_events()
    for ev in events:
        # apply ev locally (mirror of server-side reconcile)
        ...

The client uses ``urllib`` so it does NOT pull httpx/requests into the
agent's already-thin dep tree. The agent stays pure-stdlib-plus-audio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("persona.agent.sync")

_TIMEOUT = 10.0  # seconds per HTTP call

# T29 — report the agent version on every sync call (incl. heartbeat) so the
# server stores it in ``device.user_agent`` and can flag outdated installs.
# Keep in sync with ``AGENT_VERSION`` in persona_agent.py.
_AGENT_VERSION = "1.13"


@dataclass
class SyncClient:
    """Stateless thin wrapper around the server's /api/sync/* endpoints."""

    server_url: str
    device_token: str

    def _url(self, path: str) -> str:
        return f"{self.server_url.rstrip('/')}{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "X-Device-Token": self.device_token,
            "Content-Type": "application/json",
            "User-Agent": f"persona-mac-agent/{_AGENT_VERSION}",
        }

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One round-trip. Returns the decoded JSON body or ``{}``."""
        url = self._url(path)
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)

        def _sync_call() -> dict[str, Any]:
            try:
                with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                log.warning("sync_client.http_error", extra={"status": exc.code, "path": path})
                return {"error": f"http {exc.code}", "status": exc.code}
            except urllib.error.URLError as exc:
                log.warning("sync_client.transport_error", extra={"reason": str(exc.reason)})
                return {"error": str(exc.reason)}
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {"error": "bad_json"}

        return await asyncio.to_thread(_sync_call)

    async def heartbeat(self) -> dict[str, Any]:
        """Tell the server we're alive + read remote-control state.

        Response shape:
            {device_id, capture_paused, capture_interval_seconds, last_seen_at}

        The agent applies ``capture_paused`` locally (skip the next capture
        iteration), and ``capture_interval_seconds`` as a runtime override.
        """
        return await self._request("POST", "/api/devices/heartbeat", body={})

    async def state(self) -> dict[str, Any]:
        return await self._request("GET", "/api/sync/state")

    async def push_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Append local events to the server log. Best-effort; returns the
        server's response ``{appended, appended_count, skipped}``.

        Events must look like::
            {
                "kind": "note" | "tag" | "annotation" | "kv" | "shot_tag",
                "op": "insert" | "update" | "delete",
                "payload": {...},
                "logical_clock": <int millis>,
                "entity_id": optional int,
            }
        """
        if not events:
            return {"appended": [], "appended_count": 0, "skipped": 0}
        return await self._request("POST", "/api/sync/push", body={"events": events})

    async def pull_events(self, since: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Pull events with id > ``since``. Bumps the device's pull watermark
        as a side effect on the server.
        """
        path = f"/api/sync/pull?since={int(since)}&limit={int(limit)}"
        resp = await self._request("GET", path)
        events = resp.get("events") if isinstance(resp, dict) else None
        return events if isinstance(events, list) else []

    async def pull_workspace(self, since: int = 0, limit: int = 500) -> dict[str, Any]:
        """T28 — pull workspace files the AI wrote, for the code-write-target
        device. Returns the raw server response::

            {device_id, cursor, files: [{relative_path, operation, content}], count}

        On a 403 the device isn't the chosen target — the caller should
        back off quietly (the user may pick this device later). The raw
        ``{"error": ..., "status": 403}`` shape is passed straight through.
        """
        path = f"/api/workspace/sync?since={int(since)}&limit={int(limit)}"
        return await self._request("GET", path)
