"""Nightly CSV-to-webhook pipeline helpers (v1.62).

A user-configurable list of destinations, each one a (name, URL,
``csv_kind``, days_window, hour_local, headers_json) tuple persisted in
``webhook_csv_destination`` (migration ``147``). Once an hour the
companion worker (:mod:`app.workers.webhook_csv_worker`) walks the rows
whose ``hour_local`` matches the current wall-clock and POSTs a fresh
CSV dump to each destination URL. The CSV body itself is reused
verbatim from the v1.51 streaming exports in :mod:`app.csv_export` —
we just collect the async generator's chunks into a single ``str``
before handing the bytes to the HTTP client.

Why stdlib :mod:`urllib.request` and not :mod:`httpx`
-----------------------------------------------------

The rest of Persona's outbound HTTP (``app.outbox``, ``app.webhooks``)
goes through ``httpx`` because those callers need streaming, retries
and structured logging of the request body. This pipeline does not —
each call is a one-shot ``POST`` of an already-buffered body, and we
already gave ``httpx`` a fair shake (compare ``app.webhooks.signing``).
Pulling another dep just to send a single ``POST`` per destination per
night is overkill, and stdlib means one fewer transitive failure mode
during the nightly tick. The cost is that we run the synchronous
:mod:`urllib.request` blob inside :func:`asyncio.to_thread` so the
event loop stays responsive.

Privacy contract
----------------

``webhook_url`` is treated as a secret. It can carry a Notion token,
a Zapier hook id or a Google Apps Script ``?token=...`` query
parameter — any of which would deanonymise the install if it leaked
into a log line or an HTTP error message. The logger therefore never
emits the URL; it only emits the destination's surrogate ``id`` + the
opaque ``name``. The ``last_error`` column persists urllib's
``reason`` string verbatim — those strings are produced by stdlib and
do not embed the request URL.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from app.csv_export import (
    stream_audio_segments_csv,
    stream_hourly_cards_csv,
    stream_notes_csv,
    stream_screenshots_csv,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.webhook_csv_pipeline")

# Mapping from the persisted ``csv_kind`` string to the streamer that
# produces the body. Kept here (not in :mod:`app.csv_export`) so the
# CSV-export module stays unaware of *who* is consuming its output —
# the streamers are also reused by the HTTP routes in
# :mod:`app.web.routes.csv_export` without the dispatch table.
_KIND_TO_STREAMER: dict[str, Any] = {
    "screenshots": stream_screenshots_csv,
    "notes": stream_notes_csv,
    "hourly_cards": stream_hourly_cards_csv,
    "audio_segments": stream_audio_segments_csv,
}

# Network timeout for the urllib POST. Long enough that a slow GAS
# trigger (Google Apps Script can take 20 s to wake a cold script) is
# given a fair chance, short enough that a stuck connection does not
# wedge the worker for the whole hour-cycle.
_REQUEST_TIMEOUT_S: float = 60.0

# Allowed ``csv_kind`` values — duplicated from the migration's
# ``CHECK`` so the upsert can reject typos before SQLite does. Keeps
# the error message readable ("unknown csv_kind 'screensohts'")
# instead of bubbling up a raw IntegrityError.
_ALLOWED_KINDS: frozenset[str] = frozenset(_KIND_TO_STREAMER.keys())


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


async def list_destinations() -> list[dict[str, Any]]:
    """Return every row in ``webhook_csv_destination`` as a plain dict.

    Ordered by ``name`` so the settings page renders a stable list.
    ``webhook_url`` is included because the settings page needs to show
    the user *something* to identify each row by — the UI is local-only
    and protected by the operator's auth layer, so the URL is fine to
    surface there. Loggers must still avoid it (see module docstring).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, webhook_url, csv_kind, days_window, "
            "       enabled, hour_local, headers_json, created_at, "
            "       last_sent_at, last_status_code, last_error "
            "FROM webhook_csv_destination "
            "ORDER BY name ASC"
        )
        rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "webhook_url": str(row["webhook_url"]),
                "csv_kind": str(row["csv_kind"]),
                "days_window": int(row["days_window"]),
                "enabled": bool(int(row["enabled"])),
                "hour_local": int(row["hour_local"]),
                "headers_json": (
                    None
                    if row["headers_json"] is None
                    else str(row["headers_json"])
                ),
                "created_at": str(row["created_at"]),
                "last_sent_at": (
                    None
                    if row["last_sent_at"] is None
                    else str(row["last_sent_at"])
                ),
                "last_status_code": (
                    None
                    if row["last_status_code"] is None
                    else int(row["last_status_code"])
                ),
                "last_error": (
                    None
                    if row["last_error"] is None
                    else str(row["last_error"])
                ),
            }
        )
    return out


async def upsert_destination(
    name: str,
    webhook_url: str,
    csv_kind: str,
    days_window: int,
    hour_local: int,
    enabled: bool,
    headers_json: str | None,
) -> int:
    """Insert or update one destination, return the row id.

    Keyed on ``UNIQUE(name)`` so the same name re-submitted from the
    settings form overwrites the URL/kind/window/etc. rather than
    growing a duplicate row. Input is validated up-front so the
    settings UI can echo a useful 400 instead of bubbling up the raw
    SQLite ``IntegrityError``.
    """
    name_clean = name.strip()
    url_clean = webhook_url.strip()
    kind_clean = csv_kind.strip()
    if not name_clean:
        msg = "name must be non-empty"
        raise ValueError(msg)
    if not url_clean:
        msg = "webhook_url must be non-empty"
        raise ValueError(msg)
    if not (url_clean.startswith("http://") or url_clean.startswith("https://")):
        msg = "webhook_url must start with http:// or https://"
        raise ValueError(msg)
    if kind_clean not in _ALLOWED_KINDS:
        msg = (
            f"unknown csv_kind {kind_clean!r}; "
            f"expected one of {sorted(_ALLOWED_KINDS)}"
        )
        raise ValueError(msg)
    if int(days_window) <= 0:
        msg = "days_window must be a positive integer"
        raise ValueError(msg)
    if not 0 <= int(hour_local) <= 23:
        msg = "hour_local must be 0..23"
        raise ValueError(msg)

    headers_clean: str | None
    if headers_json is None or not headers_json.strip():
        headers_clean = None
    else:
        # Parse + re-serialise so a malformed JSON blob is rejected at
        # insert time, and so the persisted string is a canonical form
        # (no leading/trailing whitespace, sorted keys not enforced
        # because Notion is sensitive to ``Notion-Version`` casing).
        try:
            parsed_headers = json.loads(headers_json)
        except json.JSONDecodeError as exc:
            msg = f"headers_json is not valid JSON: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(parsed_headers, dict):
            msg = "headers_json must decode to a JSON object"
            raise ValueError(msg)
        # Re-emit with separators that mirror :func:`json.dumps`
        # defaults — readable diffs in the DB if someone inspects it.
        headers_clean = json.dumps(parsed_headers)

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO webhook_csv_destination "
            "(name, webhook_url, csv_kind, days_window, enabled, "
            " hour_local, headers_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "    webhook_url = excluded.webhook_url, "
            "    csv_kind = excluded.csv_kind, "
            "    days_window = excluded.days_window, "
            "    enabled = excluded.enabled, "
            "    hour_local = excluded.hour_local, "
            "    headers_json = excluded.headers_json",
            (
                name_clean,
                url_clean,
                kind_clean,
                int(days_window),
                1 if enabled else 0,
                int(hour_local),
                headers_clean,
            ),
        )
        cursor = await conn.execute(
            "SELECT id FROM webhook_csv_destination WHERE name = ?",
            (name_clean,),
        )
        row = await cursor.fetchone()
        await conn.commit()
    if row is None:
        msg = "upsert_destination: row vanished mid-transaction"
        raise RuntimeError(msg)
    dest_id = int(row["id"])
    # Deliberately omit ``webhook_url`` from the log payload — see
    # the privacy contract in the module docstring.
    log.info(
        "webhook_csv.upsert",
        dest_id=dest_id,
        name=name_clean,
        csv_kind=kind_clean,
        days_window=int(days_window),
        hour_local=int(hour_local),
        enabled=bool(enabled),
        has_headers=headers_clean is not None,
    )
    return dest_id


async def delete_destination(dest_id: int) -> None:
    """Hard-delete one destination by id. No-ops on a missing id."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM webhook_csv_destination WHERE id = ?",
            (int(dest_id),),
        )
        await conn.commit()
    log.info("webhook_csv.delete", dest_id=int(dest_id))


# ---------------------------------------------------------------------------
# Send pipeline
# ---------------------------------------------------------------------------


async def _collect_csv_body(
    csv_kind: str,
    date_from: str | None,
    date_to: str | None,
) -> str:
    """Drain the chosen streamer into a single ``str``.

    The streamers in :mod:`app.csv_export` are async generators built
    for HTTP streaming responses; here we need the whole body in
    memory so we can ``Content-Length`` it for urllib. For the windows
    a nightly dump uses (one to seven days of one user's data),
    drained CSVs stay in the low single-digit MB range — well below
    any practical memory ceiling.
    """
    streamer = _KIND_TO_STREAMER[csv_kind]
    chunks: list[str] = []
    async for chunk in streamer(date_from, date_to):
        chunks.append(chunk)
    return "".join(chunks)


def _post_csv(
    url: str,
    body_bytes: bytes,
    extra_headers: dict[str, str],
) -> tuple[int, str | None]:
    """Synchronous urllib POST, returns ``(status_code, error_or_None)``.

    Runs inside :func:`asyncio.to_thread` so the event loop is not
    blocked. The signature returns either ``(status, None)`` on a
    completed HTTP exchange — including 4xx/5xx, those are still
    "the destination answered" — or ``(0, reason)`` when the
    transport never produced a status code (DNS / connect / TLS
    failure). The ``0`` sentinel matches what curl writes for the
    same condition.
    """
    headers: dict[str, str] = {"Content-Type": "text/csv"}
    headers.update(extra_headers)
    request = urllib.request.Request(  # noqa: S310 - URL is operator-supplied
        url,
        data=body_bytes,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL is operator-supplied
            request, timeout=_REQUEST_TIMEOUT_S
        ) as response:
            # ``HTTPResponse.status`` is always present in 3.9+.
            return int(response.status), None
    except urllib.error.HTTPError as exc:
        # 4xx/5xx — the destination replied with a refusal. Still a
        # successful round-trip from the pipeline's perspective: we
        # know the status, we know it failed at the application layer.
        return int(exc.code), None
    except urllib.error.URLError as exc:
        # DNS / connect / TLS — no status code. ``reason`` is a
        # stdlib-formatted string and does not embed the URL.
        return 0, str(exc.reason)
    except TimeoutError as exc:
        # Raised directly by socket layer on a connect/read timeout
        # under Python 3.10+. Surfaced as the ``0`` sentinel so the
        # UI can render it consistently with a connect failure.
        return 0, f"timeout: {exc}"


def _parse_headers_json(raw: str | None) -> dict[str, str]:
    """Decode the stored ``headers_json`` blob into a header dict.

    ``None`` / empty / parse-failure all return an empty dict; the
    pipeline still goes ahead with the default ``Content-Type`` header.
    Defensive parsing here keeps a malformed row (e.g. a hand-edited
    DB) from killing the worker mid-tick.
    """
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning(
            "webhook_csv.headers_json.bad",
            error=str(exc),
        )
        return {}
    if not isinstance(parsed, dict):
        log.warning(
            "webhook_csv.headers_json.not_object",
            type=type(parsed).__name__,
        )
        return {}
    out: dict[str, str] = {}
    for key, value in parsed.items():
        out[str(key)] = str(value)
    return out


def _resolve_date_window(
    now: datetime,
    days_window: int,
) -> tuple[str, str]:
    """Return ``(date_from, date_to)`` for the streamer's window.

    The window is a half-open ``[today - days_window, today)`` range
    expressed in local-clock YYYY-MM-DD strings — which is the input
    shape every CSV streamer accepts (see
    :func:`app.csv_export._build_date_filter`). For ``days_window=1``
    that is "yesterday only", the canonical nightly incremental.
    """
    today = now.date()
    start = today - timedelta(days=int(days_window))
    return start.isoformat(), today.isoformat()


def _parse_now_iso(now_iso: str | None) -> datetime:
    """Parse ``now_iso`` or fall back to wall-clock.

    The worker always passes a real ISO string; the fall-back exists
    so the ``send-now`` endpoint can pass ``None`` without a separate
    code path. Naive datetimes are assumed local-time (matches what
    ``datetime.now().astimezone()`` produces elsewhere in the
    codebase).
    """
    if now_iso is None or not now_iso.strip():
        return datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(now_iso.strip())
    except ValueError:
        log.warning("webhook_csv.bad_now", value=now_iso)
        return datetime.now().astimezone()
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


async def send_destination(
    dest_id: int,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Build the CSV, POST it, persist the outcome, return a status dict.

    The return shape is::

        {
            "status": "sent" | "http_error" | "transport_error"
                       | "missing" | "disabled",
            "status_code": int,         # 0 when no HTTP reply at all
            "body_bytes_sent": int,     # CSV body size on the wire
        }

    The ``missing`` outcome covers a stale ``send-now`` call against an
    id that has just been deleted; the ``disabled`` outcome covers a
    worker tick that races with the operator toggling a row off
    (worker fan-out does its own enabled filter, but the one-shot
    ``send-now`` reuses this helper and must not bypass it).

    The function ALWAYS writes a row to ``last_sent_at`` /
    ``last_status_code`` / ``last_error`` so the settings UI can show
    "we tried at 05:00 and got X" even when X is a transport failure.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, webhook_url, csv_kind, days_window, "
            "       enabled, headers_json "
            "FROM webhook_csv_destination "
            "WHERE id = ?",
            (int(dest_id),),
        )
        row = await cursor.fetchone()

    if row is None:
        log.warning("webhook_csv.send.missing", dest_id=int(dest_id))
        return {"status": "missing", "status_code": 0, "body_bytes_sent": 0}

    if not bool(int(row["enabled"])):
        log.info("webhook_csv.send.disabled", dest_id=int(dest_id))
        return {"status": "disabled", "status_code": 0, "body_bytes_sent": 0}

    name = str(row["name"])
    url = str(row["webhook_url"])
    csv_kind = str(row["csv_kind"])
    days_window = int(row["days_window"])
    headers_raw = (
        None if row["headers_json"] is None else str(row["headers_json"])
    )

    now = _parse_now_iso(now_iso)
    date_from, date_to = _resolve_date_window(now, days_window)

    log.info(
        "webhook_csv.send.start",
        dest_id=int(dest_id),
        name=name,
        csv_kind=csv_kind,
        date_from=date_from,
        date_to=date_to,
    )

    body_text = await _collect_csv_body(csv_kind, date_from, date_to)
    body_bytes = body_text.encode("utf-8")

    extra_headers = _parse_headers_json(headers_raw)

    status_code, transport_error = await asyncio.to_thread(
        _post_csv, url, body_bytes, extra_headers
    )

    # Stamp the outcome regardless of success/failure. ``last_sent_at``
    # always advances — the operator wants "when did we last try?",
    # not "when did we last succeed?".
    now_iso_stamp = (
        datetime.now(UTC).isoformat()
        if now is None
        else now.isoformat()
    )
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE webhook_csv_destination "
            "SET last_sent_at = ?, "
            "    last_status_code = ?, "
            "    last_error = ? "
            "WHERE id = ?",
            (
                now_iso_stamp,
                int(status_code) if status_code else None,
                transport_error,
                int(dest_id),
            ),
        )
        await conn.commit()

    if transport_error is not None:
        log.warning(
            "webhook_csv.send.transport_error",
            dest_id=int(dest_id),
            name=name,
            error=transport_error,
            body_bytes=len(body_bytes),
        )
        return {
            "status": "transport_error",
            "status_code": 0,
            "body_bytes_sent": len(body_bytes),
        }

    if 200 <= int(status_code) < 300:
        log.info(
            "webhook_csv.send.ok",
            dest_id=int(dest_id),
            name=name,
            status_code=int(status_code),
            body_bytes=len(body_bytes),
        )
        return {
            "status": "sent",
            "status_code": int(status_code),
            "body_bytes_sent": len(body_bytes),
        }

    log.warning(
        "webhook_csv.send.http_error",
        dest_id=int(dest_id),
        name=name,
        status_code=int(status_code),
        body_bytes=len(body_bytes),
    )
    return {
        "status": "http_error",
        "status_code": int(status_code),
        "body_bytes_sent": len(body_bytes),
    }


__all__ = [
    "delete_destination",
    "list_destinations",
    "send_destination",
    "upsert_destination",
]
