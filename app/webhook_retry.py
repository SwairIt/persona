"""Retry queue for failed outbound webhook deliveries.

The dispatcher (:mod:`app.webhooks.dispatcher`) calls :func:`enqueue` on
its initial-attempt failure path (5xx from the receiver or a transport
error such as ``httpx.ConnectError`` / ``httpx.ReadTimeout``). The row
carries the raw signed envelope bytes so a background worker
(:mod:`app.workers.webhook_retry_worker`) can re-POST them later.

The retry strategy is exponential backoff in *minutes*: attempt N waits
``2 ** attempts`` minutes before firing, capped at
:data:`MAX_BACKOFF_SECONDS` (24h) and at :data:`MAX_ATTEMPTS` (8) total
attempts. On success the row is deleted; on a further failure the row
stays put with ``attempts += 1``, ``next_attempt_at`` advanced and
``last_error`` set to the truncated error string. When the cap is hit
the row is deleted — we deliberately don't keep a "dead-letter" table;
the dispatcher already records the original failure on the
``webhooks.last_error`` column for operator visibility.

Signing notes: every retry re-runs :func:`app.webhook_signing.sign_payload`
because the per-webhook secret could have rotated since the original
attempt (unlikely today — secrets are write-once — but the wire format
demands the signature match whatever secret the receiver currently has).
The original ``ts`` inside the body is preserved verbatim, so the
``X-Persona-Timestamp`` header keeps its happens-at-emission value and
receivers can still enforce replay protection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypedDict

import httpx

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.webhook_signing import ensure_secret, sign_payload

if TYPE_CHECKING:
    import aiosqlite


class _QueueRow(TypedDict):
    """Materialised row from ``webhook_retry_queue`` — typed so the
    replay path can read fields without ``cast()`` clutter."""

    id: int
    webhook_id: int
    event_type: str
    body: bytes
    attempts: int

log = get_logger("persona.webhook.retry")

MAX_ATTEMPTS: Final[int] = 8
"""Hard ceiling on retry attempts per row.

Eight attempts with ``2**attempts``-minute spacing covers roughly
``2+4+8+16+32+64+128+256 ≈ 8.5 hours`` (the last few are capped at
:data:`MAX_BACKOFF_SECONDS`). After this the row is dropped — a
receiver that's been broken for nine hours has bigger problems than a
missed event and we don't want the queue growing unbounded.
"""

MAX_BACKOFF_SECONDS: Final[int] = 24 * 60 * 60
"""Maximum gap between two retries — 24h.

``2 ** 8 == 256`` minutes is well under this cap; the constant exists so
the formula never overflows past a reasonable upper bound if
:data:`MAX_ATTEMPTS` is bumped in the future.
"""

_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0
"""Per-request timeout for the replay POST — matches the dispatcher."""

_ERROR_TRUNCATE: Final[int] = 200
"""Cap on ``last_error`` length so a multi-MB stack trace can't bloat
the row. Matches the dispatcher's existing truncation."""

_BATCH_SIZE: Final[int] = 50
"""Upper bound on rows drained per :func:`process_queue` call.

Bounds the worker's per-tick wall-clock so a backlog of thousands can't
hold the polling loop for minutes — the next tick will pick up the
remainder. Smaller than the OCR worker's batch because each retry
involves an outbound HTTP round-trip.
"""


async def enqueue(webhook_id: int, event_type: str, body: bytes) -> None:
    """Persist a failed delivery for later retry.

    Called by the dispatcher on its initial-attempt failure path. The
    row's ``next_attempt_at`` defaults to ``datetime('now')`` so the
    very first retry will fire on the worker's next poll — there's no
    explicit "wait a minute before the first retry" because the worker's
    own 1-minute polling cadence already provides that delay.
    """
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO webhook_retry_queue "
            "(webhook_id, event_type, body, attempts) "
            "VALUES (?, ?, ?, 0)",
            (webhook_id, event_type, body),
        )
        await conn.commit()
    log.info(
        "webhook.retry.enqueued",
        webhook_id=webhook_id,
        event_type=event_type,
        body_bytes=len(body),
    )


async def process_queue() -> None:
    """Drain due rows from the retry queue.

    Picks rows where ``next_attempt_at <= now`` and
    ``attempts < MAX_ATTEMPTS``, oldest-due first, replays each via
    HTTP POST and either deletes the row on success or schedules the
    next attempt on failure. A row that hits the attempt cap is removed
    so the queue never grows unbounded.

    Exceptions inside a single row's replay are caught and logged — one
    bad receiver must never silence the rest of the queue.
    """
    rows = await _claim_due_rows(_BATCH_SIZE)
    if not rows:
        return

    log.debug("webhook.retry.batch", row_count=len(rows))

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        for row in rows:
            try:
                await _replay_one(client, row)
            except Exception as exc:  # defensive: never let one row kill the worker
                log.exception(
                    "webhook.retry.row_failed",
                    row_id=row["id"],
                    webhook_id=row["webhook_id"],
                    error=str(exc),
                )


async def _replay_one(client: httpx.AsyncClient, row: _QueueRow) -> None:
    """Re-POST one row's body to its webhook URL.

    On success (2xx) the row is deleted. On 5xx or a transport error we
    bump ``attempts`` and reschedule; if the bump reaches
    :data:`MAX_ATTEMPTS` the row is deleted instead (giving up). A 4xx
    response is treated as terminal — the receiver actively rejected
    the payload, no amount of retrying will fix it — and the row is
    deleted.
    """
    row_id = row["id"]
    webhook_id = row["webhook_id"]
    attempts = row["attempts"]
    body = row["body"]
    event_type = row["event_type"]

    url = await _lookup_url(webhook_id)
    if url is None:
        # Webhook was deleted (or disabled and removed) while the row
        # was queued — drop the orphan rather than keep it forever.
        await _delete_row(row_id)
        log.info(
            "webhook.retry.orphan_dropped",
            row_id=row_id,
            webhook_id=webhook_id,
        )
        return

    try:
        secret = await ensure_secret(webhook_id)
    except LookupError:
        await _delete_row(row_id)
        log.info(
            "webhook.retry.orphan_dropped",
            row_id=row_id,
            webhook_id=webhook_id,
        )
        return

    ts_header = _extract_timestamp(body)
    headers = {
        "Content-Type": "application/json",
        "X-Persona-Signature": sign_payload(secret, body),
    }
    if ts_header is not None:
        headers["X-Persona-Timestamp"] = ts_header

    try:
        response = await client.post(url, content=body, headers=headers)
    except (httpx.HTTPError, TimeoutError) as exc:
        await _record_failure(row_id, attempts, error=str(exc))
        log.warning(
            "webhook.retry.transport_error",
            row_id=row_id,
            webhook_id=webhook_id,
            attempts=attempts,
            error=str(exc),
        )
        return

    status = response.status_code
    if 200 <= status < 300:
        await _delete_row(row_id)
        log.info(
            "webhook.retry.delivered",
            row_id=row_id,
            webhook_id=webhook_id,
            event_type=event_type,
            attempts=attempts + 1,
            status=status,
        )
        return

    if 500 <= status < 600:
        await _record_failure(
            row_id,
            attempts,
            error=f"HTTP {status}",
        )
        log.warning(
            "webhook.retry.server_error",
            row_id=row_id,
            webhook_id=webhook_id,
            attempts=attempts,
            status=status,
        )
        return

    # 4xx (and exotic 1xx/3xx) — receiver actively said no. Drop the
    # row; we won't get a different answer by retrying.
    await _delete_row(row_id)
    log.warning(
        "webhook.retry.terminal",
        row_id=row_id,
        webhook_id=webhook_id,
        status=status,
    )


async def _claim_due_rows(limit: int) -> list[_QueueRow]:
    """Return rows whose ``next_attempt_at`` is in the past and under the cap.

    Reads + materialises into a list so the connection can close before
    we start the (potentially long-running) HTTP loop. Two concurrent
    workers would re-fetch the same rows — but the design assumes a
    single worker process, matching every other queue in :mod:`app.workers`.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, webhook_id, event_type, body, attempts "
            "FROM webhook_retry_queue "
            "WHERE next_attempt_at <= datetime('now') AND attempts < ? "
            "ORDER BY next_attempt_at ASC, id ASC "
            "LIMIT ?",
            (MAX_ATTEMPTS, limit),
        )
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: aiosqlite.Row) -> _QueueRow:
    return _QueueRow(
        id=int(row["id"]),
        webhook_id=int(row["webhook_id"]),
        event_type=str(row["event_type"]),
        body=bytes(row["body"]),
        attempts=int(row["attempts"]),
    )


async def _lookup_url(webhook_id: int) -> str | None:
    """Resolve the receiver URL or ``None`` if the webhook was deleted/disabled."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT url, enabled FROM webhooks WHERE id = ?",
            (webhook_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    if not bool(row["enabled"]):
        return None
    return str(row["url"])


async def _delete_row(row_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM webhook_retry_queue WHERE id = ?",
            (row_id,),
        )
        await conn.commit()


async def _record_failure(row_id: int, attempts: int, *, error: str) -> None:
    """Advance ``attempts`` and ``next_attempt_at``; drop the row if capped."""
    next_attempts = attempts + 1
    truncated = error[:_ERROR_TRUNCATE]
    if next_attempts >= MAX_ATTEMPTS:
        await _delete_row(row_id)
        log.warning(
            "webhook.retry.gave_up",
            row_id=row_id,
            attempts=next_attempts,
            error=truncated,
        )
        return

    backoff_minutes = _backoff_minutes(next_attempts)
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE webhook_retry_queue SET "
            "attempts = ?, "
            "next_attempt_at = datetime('now', ?), "
            "last_error = ? "
            "WHERE id = ?",
            (
                next_attempts,
                f"+{backoff_minutes} minutes",
                truncated,
                row_id,
            ),
        )
        await conn.commit()


def _backoff_minutes(attempts: int) -> int:
    """Return ``2 ** attempts`` minutes, capped at :data:`MAX_BACKOFF_SECONDS`.

    ``attempts`` is the *post-increment* value, i.e. the number of
    delivery attempts the row will have once this retry is counted. The
    formula gives ``2, 4, 8, 16, 32, 64, 128, 256`` minutes for
    attempts 1..8 — the upper end is hardware-clamped to 1440 minutes
    (24h) so a future bump of :data:`MAX_ATTEMPTS` can't yield
    multi-day waits.
    """
    raw_seconds: int = (2**attempts) * 60
    capped_seconds: int = raw_seconds if raw_seconds < MAX_BACKOFF_SECONDS else MAX_BACKOFF_SECONDS
    return capped_seconds // 60


def _extract_timestamp(body: bytes) -> str | None:
    """Pull the ``ts`` field out of a dispatcher envelope, if present.

    The dispatcher always emits ``{"event", "ts", "payload"}``; we use
    the original timestamp on every replay so receivers see a stable
    ``X-Persona-Timestamp`` header that matches the body. Decoding
    failures fall back to ``None`` — better to drop the header than to
    fabricate a new timestamp that disagrees with the body.
    """
    import json  # noqa: PLC0415 — local import keeps module-level cost minimal

    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    ts = decoded.get("ts")
    return str(ts) if isinstance(ts, str) else None


__all__ = [
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_SECONDS",
    "enqueue",
    "process_queue",
]
