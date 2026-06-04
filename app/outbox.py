"""Outbox dispatcher for service-specific webhook fan-out.

Persona's first-party webhook system (``app.webhooks.dispatcher``) emits
a signed ``{"event", "ts", "payload"}`` envelope to URLs the operator
trusts to parse it. Linear and Notion don't speak that envelope — they
expect their own GraphQL / REST shapes with bearer-token auth. This
module lets the operator pre-bake one row per integration in
``outbox_template`` (see migration 111) and fans events out by rendering
``body_template`` with :py:meth:`str.format_map` against the payload,
then POSTing with the per-row ``Authorization`` header.

Active ``event_kind`` values
----------------------------

The dispatcher does not enforce a vocabulary — passing an unknown
``event_kind`` simply matches zero rows and returns ``0``. The list
below is the contract the rest of the codebase emits today; operators
designing templates should expect these payload shapes:

* ``shot_pinned`` —
  ``{"shot_id": int, "captured_at": str, "app": str}``
  Fired by the manual pin route (:mod:`app.web.routes.pin`).
* ``weekly_digest_ready`` —
  ``{"week_iso": str, "url": str}``
  Fired by the weekly card worker.
* ``daily_digest_ready`` —
  ``{"date": str, "url": str}``
  Fired by the daily card worker.
* ``note_created`` —
  ``{"note_id": int, "title": str}``
  Fired by the encrypted-notes create endpoint.
* ``alt_text_added`` —
  ``{"shot_id": int, "alt_text": str}``
  Fired by the shot alt-text endpoint.

Rendering failures
------------------

``str.format_map`` raises :class:`KeyError` when a template references a
placeholder the payload doesn't supply, and :class:`IndexError` /
:class:`ValueError` for malformed format specs. We catch both, log a
warning naming the offending template, and skip it — one bad template
must never block sibling templates from delivering.

Retry coupling
--------------

The existing ``webhook_retry_queue`` was built around
``webhook_subscription.id`` as a foreign key — the retry worker looks
up the receiver URL via that id. Outbox templates live in a separate
table with a different id space, so we cannot insert into the queue
without breaking the worker's lookup. The compromise: log every
non-2xx response loudly so the operator sees the failure on their log
shipper, but accept that the *first* dispatch attempt is the only one.
Wiring outbox-aware retry is tracked as follow-up; the spec for this
feature explicitly accepted "fire once, log on failure" semantics.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.outbox")

_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0
"""Per-request timeout. Matches :mod:`app.webhooks.dispatcher` so a
template that points at a slow receiver doesn't behave differently from
a native subscriber pointing at the same host."""

_KNOWN_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "shot_pinned",
        "weekly_digest_ready",
        "daily_digest_ready",
        "note_created",
        "alt_text_added",
    },
)
"""Documented event_kind vocabulary — exported so the admin UI can list
the supported events without re-typing them. The dispatcher itself does
not gate on this set; an unknown kind matches zero rows and returns
``0`` fanouts."""


class _Template:
    """Materialised row from ``outbox_template`` for the hot path."""

    __slots__ = ("auth_header", "body_template", "id", "name", "service", "target_url")

    def __init__(
        self,
        *,
        id_: int,
        name: str,
        service: str,
        target_url: str,
        auth_header: str | None,
        body_template: str,
    ) -> None:
        self.id = id_
        self.name = name
        self.service = service
        self.target_url = target_url
        self.auth_header = auth_header
        self.body_template = body_template


async def list_active_templates(event_kind: str) -> list[dict[str, Any]]:
    """Return enabled templates for ``event_kind``.

    Used by the admin UI to show "which templates would fire if X
    happened right now?" and by :func:`dispatch_event` internally. The
    return type is plain ``dict`` so the admin UI can serialise it
    straight to JSON without an adapter layer.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, service, event_kind, target_url, auth_header, "
            "body_template, enabled, created_at "
            "FROM outbox_template "
            "WHERE event_kind = ? AND enabled = 1 "
            "ORDER BY id ASC",
            (event_kind,),
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "service": str(row["service"]),
            "event_kind": str(row["event_kind"]),
            "target_url": str(row["target_url"]),
            "auth_header": (
                str(row["auth_header"]) if row["auth_header"] is not None else None
            ),
            "body_template": str(row["body_template"]),
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def dispatch_event(event_kind: str, payload: dict[str, Any]) -> int:
    """Fan ``payload`` out to every enabled template for ``event_kind``.

    Returns the number of templates that produced an HTTP request — a
    template that fails to render (placeholder missing in ``payload``)
    is *not* counted, since no request was sent. A template that sent a
    request but received a non-2xx is counted: it still consumed the
    fan-out slot.

    The HTTP client is created once per call rather than once per
    template so connection pooling kicks in if two templates point at
    the same host.
    """
    if not payload:
        # Empty payloads still render literal-only templates, but they
        # are almost always a caller bug — surface it once at debug.
        log.debug("outbox.dispatch.empty_payload", event_kind=event_kind)

    rows = await list_active_templates(event_kind)
    if not rows:
        return 0

    templates = [
        _Template(
            id_=int(row["id"]),
            name=str(row["name"]),
            service=str(row["service"]),
            target_url=str(row["target_url"]),
            auth_header=(
                str(row["auth_header"]) if row["auth_header"] is not None else None
            ),
            body_template=str(row["body_template"]),
        )
        for row in rows
    ]

    fanouts = 0
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        for tmpl in templates:
            sent = await _fire_one(client, event_kind, tmpl, payload)
            if sent:
                fanouts += 1
    return fanouts


async def _fire_one(
    client: httpx.AsyncClient,
    event_kind: str,
    tmpl: _Template,
    payload: dict[str, Any],
) -> bool:
    """POST one rendered template. Returns ``True`` if the request was sent.

    A ``False`` return means we never made the request — render
    failure, not delivery failure. Delivery failures (non-2xx, network
    error) are logged at warning level but still count as "sent" from
    the caller's perspective.
    """
    try:
        body = tmpl.body_template.format_map(payload)
    except (KeyError, IndexError, ValueError) as exc:
        log.warning(
            "outbox.render_failed",
            template_id=tmpl.id,
            template_name=tmpl.name,
            event_kind=event_kind,
            error=str(exc),
        )
        return False

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if tmpl.auth_header is not None:
        headers["Authorization"] = tmpl.auth_header

    try:
        response = await client.post(tmpl.target_url, content=body, headers=headers)
    except (httpx.HTTPError, TimeoutError) as exc:
        log.warning(
            "outbox.transport_error",
            template_id=tmpl.id,
            template_name=tmpl.name,
            event_kind=event_kind,
            service=tmpl.service,
            error=str(exc),
        )
        await _enqueue_retry_best_effort(tmpl, event_kind, body, error=str(exc))
        return True

    status = response.status_code
    if 200 <= status < 300:
        log.info(
            "outbox.delivered",
            template_id=tmpl.id,
            template_name=tmpl.name,
            event_kind=event_kind,
            service=tmpl.service,
            status=status,
        )
        return True

    log.warning(
        "outbox.non_2xx",
        template_id=tmpl.id,
        template_name=tmpl.name,
        event_kind=event_kind,
        service=tmpl.service,
        status=status,
    )
    await _enqueue_retry_best_effort(
        tmpl,
        event_kind,
        body,
        error=f"HTTP {status}",
    )
    return True


async def _enqueue_retry_best_effort(
    tmpl: _Template,
    event_kind: str,
    body: str,
    *,
    error: str,
) -> None:
    """Insert a retry row into ``webhook_retry_queue`` if we can.

    The existing queue's ``webhook_id`` column points at the
    ``webhook_subscription`` table — outbox templates live in a
    different table, so we have no compatible id to supply. We attempt
    the insert with ``webhook_id = 0`` (a guaranteed-orphan id); the
    retry worker's ``_lookup_url`` will return ``None`` and drop the
    row, but the insert itself succeeds and gives the operator a paper
    trail in the queue table for triage.

    Any database error here is swallowed: best-effort by design — the
    primary signal (the warning above) already fired.
    """
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO webhook_retry_queue "
                "(webhook_id, event_type, body, attempts, last_error) "
                "VALUES (?, ?, ?, 0, ?)",
                (
                    0,
                    f"outbox.{event_kind}.template_{tmpl.id}",
                    body.encode("utf-8"),
                    error[:200],
                ),
            )
            await conn.commit()
    except Exception as exc:  # best-effort logging, never let it bubble
        log.debug(
            "outbox.retry_enqueue_failed",
            template_id=tmpl.id,
            error=str(exc),
        )


__all__ = [
    "_KNOWN_EVENT_KINDS",
    "dispatch_event",
    "list_active_templates",
]
