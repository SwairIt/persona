"""Best-effort fire-and-forget HTTP POST to subscribed webhook URLs.

Each webhook receives a JSON body:

    {
      "event": "<event_type>",
      "ts": "<ISO 8601 timestamp>",
      "payload": { ... arbitrary fields per event type ... }
    }

Every delivery is signed. The dispatcher always attaches:

* ``X-Persona-Signature: sha256=<hexdigest>`` — HMAC-SHA256 of the raw
  request body keyed by the per-webhook secret. The secret is generated
  on demand (``secrets.token_urlsafe(32)``) the first time a webhook
  fires, persisted, and reused for all future deliveries — see
  :func:`app.webhook_signing.ensure_secret`.
* ``X-Persona-Timestamp`` — the same ISO-8601 UTC timestamp that appears
  inside the body, hoisted to a header so receivers can implement replay
  protection without having to parse JSON first.

Failures (non-2xx, timeout, connection error) are recorded in the
``webhooks`` table for visibility on the Webhooks settings page. We
never retry — keep it dumb and predictable.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.webhooks import list_webhooks, record_delivery
from app.webhook_filters import should_fire
from app.webhook_signing import ensure_secret, sign_payload

log = get_logger("persona.webhook.sign")
filter_log = get_logger("persona.webhook.filters")

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

VALID_EVENTS = frozenset(
    [
        "capture.saved",
        "capture.budget_exceeded",
        "digest.daily_generated",
        "ocr.completed",
        "tier.demoted",
        "streak.broken",
    ]
)


async def dispatch_event(event_type: str, payload: dict[str, Any]) -> None:
    """Spawn a background task that posts the event to all enabled subscribers."""
    if event_type not in VALID_EVENTS:
        log.warning("webhook.sign.unknown_event", event_type=event_type)
        return
    # Fire-and-forget: we deliberately don't await the task. Stash a
    # strong reference in the module-level set so the GC doesn't cancel
    # it mid-flight (asyncio holds tasks weakly), then drop the
    # reference on completion. Failures are surfaced via the `webhooks`
    # row, not via the caller's stack.
    task = asyncio.create_task(_do_dispatch(event_type, payload))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _do_dispatch(event_type: str, payload: dict[str, Any]) -> None:
    # v0.44: webhooks now carry an ``event_types`` filter (comma list or
    # ``*``). We fetch *all* enabled subscribers and then apply
    # :func:`app.webhook_filters.should_fire` per-row so a single webhook
    # can subscribe to many event types in one go (or all via ``*``).
    async with get_connection() as conn:
        all_subs = await list_webhooks(conn, only_enabled=True)
    subs = [
        sub
        for sub in all_subs
        if should_fire(_event_filter(sub), event_type)
    ]
    if not subs:
        if all_subs:
            filter_log.debug(
                "webhook.filters.all_skipped",
                event_type=event_type,
                considered=len(all_subs),
            )
        return

    ts = datetime.now(UTC).isoformat()
    body_obj = {
        "event": event_type,
        "ts": ts,
        "payload": payload,
    }
    body_bytes = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [_post_one(client, sub, body_bytes, ts) for sub in subs]
        await asyncio.gather(*tasks, return_exceptions=True)


def _event_filter(sub: dict[str, Any]) -> str | None:
    """Extract the v0.44 ``event_types`` filter from a webhook row.

    Falls back to the legacy single ``event_type`` column when the new
    column is absent (older databases that haven't run migration 042
    yet) so the dispatcher behaves identically to v0.43 in that case.
    """
    raw = sub.get("event_types")
    if raw is None:
        legacy = sub.get("event_type")
        return None if legacy is None else str(legacy)
    return str(raw)


async def _post_one(
    client: httpx.AsyncClient,
    sub: dict[str, Any],
    body: bytes,
    ts: str,
) -> None:
    sub_id = int(sub["id"])
    url = str(sub["url"])

    secret = await ensure_secret(sub_id)
    headers = {
        "Content-Type": "application/json",
        "X-Persona-Signature": sign_payload(secret, body),
        "X-Persona-Timestamp": ts,
    }

    try:
        response = await client.post(url, content=body, headers=headers)
        async with get_connection() as conn:
            await record_delivery(conn, sub_id, status_code=response.status_code)
    except (httpx.HTTPError, TimeoutError) as exc:
        async with get_connection() as conn:
            await record_delivery(conn, sub_id, status_code=0, error=str(exc)[:200])
        log.warning("webhook.sign.delivery_failed", url=url, error=str(exc))


async def dispatch_test(webhook_id: int, event_type: str | None = None) -> dict[str, Any]:
    """Fire a synthetic signed event at a single webhook for manual testing.

    Looks up the webhook by id; if missing or disabled returns
    ``{"ok": False, "reason": "missing or disabled"}``. Otherwise builds a
    synthetic payload and hands it off to :func:`dispatch_event` (which
    spawns a background task and never blocks). The receiver will see the
    exact same signed envelope as a real event, so this is the canonical
    way for users to debug their receiver-side verification.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, url, event_type, enabled FROM webhooks WHERE id = ?",
            (webhook_id,),
        )
        row = await cursor.fetchone()

    if row is None or not bool(row["enabled"]):
        return {"ok": False, "reason": "missing or disabled"}

    chosen = event_type if (event_type and event_type in VALID_EVENTS) else str(row["event_type"])
    if chosen not in VALID_EVENTS:
        chosen = "capture.saved"

    payload = {
        "screenshot_id": 0,
        "test": True,
        "fired_at": datetime.now(UTC).isoformat(),
    }
    await dispatch_event(chosen, payload)
    return {"ok": True, "event_type": chosen}
