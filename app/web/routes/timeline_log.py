"""Git-style timeline log — text-mode chronological event stream.

Three sibling endpoints, one shared data layer in
:mod:`app.timeline_log`:

* ``GET /timeline/log``           → HTML page (monospace ``<pre>``
  block + colored glyph gutter + filter-chip bar).
* ``GET /api/timeline/log.json``  → machine-readable equivalent for
  dashboards, the command palette and any future automation.
* ``GET /api/timeline/log.txt``   → flat plain-text "git log --oneline"
  flavour for piping into a terminal, ``grep`` or a feed reader. Same
  loopback-or-token gate the operator-only RSS feed already uses, since
  the log mixes capture window titles and OCR-derived note bodies and
  must not be published to the open internet.

The HTML and JSON endpoints inherit the existing session-level access
controls of the rest of the app; the ``.txt`` endpoint is the only one
gated tighter because plain-text output is the easiest to ingest into
something the operator can't always audit.

The query-string ``kind`` filter is optional and accepts a single value
(``capture``, ``note``, ``pin``, ``tag``, ``capture_event``,
``reminder``). Any other value is silently dropped — same forgiving
behaviour the timeline-filter-chip bar already uses. A bad value lands
on the unfiltered log rather than 400-ing.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main`; the task spec forbids touching ``main.py``. Wire
it up in a follow-up patch with::

    from app.web.routes import timeline_log as timeline_log_routes
    app.include_router(timeline_log_routes.router)
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.feed_tokens import verify_token
from app.logging_setup import get_logger
from app.timeline_log import (
    KIND_CATALOGUE,
    LogLine,
    build_log_lines,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["timeline-log"])

log = get_logger("persona.web.timeline_log")

# Hard ceiling on per-request rows. Matches the default in
# :func:`app.timeline_log.build_log_lines` so the HTML page and the
# JSON / text endpoints agree on what "today" looks like.
_DEFAULT_LIMIT: Final[int] = 500
_MAX_LIMIT: Final[int] = 2_000

# Set of valid ``kind`` values, derived from the canonical catalogue so
# this module never falls out of sync when a new kind ships.
_VALID_KINDS: Final[frozenset[str]] = frozenset(k for k, _, _ in KIND_CATALOGUE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_kind(value: str | None) -> str | None:
    """Return the requested ``kind`` if valid, otherwise ``None``.

    A typo or an unknown kind silently degrades to no filter rather
    than 400-ing — matches the forgiving behaviour of every other
    filter-chip endpoint in the repo.
    """
    if value is None or value == "":
        return None
    if value not in _VALID_KINDS:
        log.info("timeline_log.kind_unknown_fallback_all", value=value)
        return None
    return value


def _filter_lines(items: list[LogLine], kind: str | None) -> list[LogLine]:
    """Apply the ``kind`` chip filter in Python.

    The per-source SQL is already cheap and we don't want to push the
    chip into the merged query (it would require either UNION-ALL of
    only-the-matching source or a per-kind table-routing dispatch — a
    lot of code for a feature that's I/O-bound on SQLite). Filtering in
    Python after the merge keeps :func:`app.timeline_log.build_log_lines`
    single-purpose and the chip implementation a one-liner.
    """
    if kind is None:
        return items
    return [row for row in items if row["kind"] == kind]


def _clamp_limit(value: int | None) -> int:
    """Return a sane ``limit`` regardless of caller input.

    Mirrors the defensive clamp inside :func:`build_log_lines` so the
    JSON / text endpoints honour the same ceiling — useful when a
    caller hand-builds a URL with ``?limit=999999``.
    """
    if value is None:
        return _DEFAULT_LIMIT
    if value < 1:
        return _DEFAULT_LIMIT
    return min(value, _MAX_LIMIT)


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from ``127.0.0.1`` / ``::1``.

    Mirrors :func:`app.web.routes.audit_rss._is_loopback_client` and
    :func:`app.web.routes.metrics_export._is_loopback_client` — we
    deliberately ignore ``X-Forwarded-For`` because the plain-text
    endpoint is expected to be consumed on the same host (curl, a
    desktop feed reader, a shell pipeline).
    """
    client = request.client
    if client is None:
        return False
    try:
        return ip_address(client.host).is_loopback
    except ValueError:
        return False


async def _txt_gate_ok(request: Request) -> bool:
    """Decide whether the ``.txt`` request may proceed.

    Two paths to "yes":

    1. The request comes from a loopback address (curl / local shell).
    2. The request carries a valid feed-share token via the standard
       ``?token=`` query param, and that token's ``feed_pattern``
       matches the request path. The token table is shared with the
       existing RSS feeds, so an operator can issue a single token for
       ``/api/timeline/log.txt`` and consume it from a remote feed
       reader without opening the rest of the app.

    Logs the *outcome* but never the raw token. Same constant-time
    comparison contract as the underlying :func:`app.feed_tokens.verify_token`.
    """
    if _is_loopback_client(request):
        return True
    raw_token = request.query_params.get("token", "")
    if not raw_token:
        return False
    result = await verify_token(raw_token, request.url.path)
    # ``FeedVerifyResult`` is a ``TypedDict(total=False)`` so ``ok`` is
    # syntactically optional even though :func:`verify_token`'s docstring
    # promises the key is always present. ``.get()`` keeps mypy strict
    # happy without a cast or an inline ignore.
    return bool(result.get("ok", False))


def _format_txt(items: list[LogLine]) -> str:
    """Render the merged log as a flat plain-text stream.

    One row per line, tab-separated columns so the output is greppable
    and pipeable. Order matches the on-screen order (ts DESC).
    """
    out: list[str] = []
    for row in items:
        out.append(f"{row['ts_iso']}\t{row['glyph']} {row['kind']}\t{row['text']}")
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/timeline/log", response_class=HTMLResponse)
async def timeline_log_page(
    request: Request,
    day: str | None = Query(default=None, description="YYYY-MM-DD; default = today"),
    kind: str | None = Query(default=None, description="Filter chip"),
    limit: int | None = Query(default=None, ge=1, le=_MAX_LIMIT),
) -> HTMLResponse:
    """Render the per-day timeline log as a monospace ``<pre>`` block.

    Each line is one event, leading single-character glyph in a colour
    keyed by ``kind``. The chip bar above the pre block lets the user
    narrow to a single kind without leaving the page.
    """
    effective_limit = _clamp_limit(limit)
    kind_filter = _normalise_kind(kind)

    items = await build_log_lines(day_iso=day, limit=effective_limit)
    filtered = _filter_lines(items, kind_filter)

    log.info(
        "timeline_log.page",
        day=day or "today",
        kind=kind_filter,
        total=len(items),
        shown=len(filtered),
    )

    return templates.TemplateResponse(
        request,
        "timeline_log.html",
        {
            "title": "События дня",
            "active_nav": "timeline",
            "day": day,
            # Context key is ``lines`` rather than ``items`` because
            # ``base.html`` does ``{% set items = [...] %}`` for its nav
            # and would otherwise shadow our list, silently rendering an
            # empty log. Same defence used by the other day-views.
            "lines": filtered,
            "total": len(items),
            "shown": len(filtered),
            "limit": effective_limit,
            "current_kind": kind_filter,
            # Sequence the template needs to build the chip bar without
            # having to import the Python catalogue itself.
            "kind_catalogue": KIND_CATALOGUE,
        },
    )


@router.get("/api/timeline/log.json", response_class=JSONResponse)
async def timeline_log_json(
    request: Request,
    day: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=_MAX_LIMIT),
) -> JSONResponse:
    """Machine-readable companion to :func:`timeline_log_page`.

    Shape per item: ``{ts_iso, kind, color, glyph, text}`` — identical
    to :class:`app.timeline_log.LogLine`. The top-level envelope
    additionally carries ``day``, ``total`` (pre-filter count),
    ``shown`` (post-filter count) and ``kind`` (the filter applied).
    """
    del request  # accepted for symmetry with the HTML route; unused here
    effective_limit = _clamp_limit(limit)
    kind_filter = _normalise_kind(kind)

    items = await build_log_lines(day_iso=day, limit=effective_limit)
    filtered = _filter_lines(items, kind_filter)

    log.info(
        "timeline_log.json",
        day=day or "today",
        kind=kind_filter,
        total=len(items),
        shown=len(filtered),
    )
    return JSONResponse(
        {
            "day": day,
            "total": len(items),
            "shown": len(filtered),
            "limit": effective_limit,
            "kind": kind_filter,
            "items": list(filtered),
        }
    )


@router.get("/api/timeline/log.txt", response_class=PlainTextResponse)
async def timeline_log_txt(
    request: Request,
    day: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=_MAX_LIMIT),
) -> PlainTextResponse:
    """Flat plain-text log — loopback-only or with a valid feed token.

    The text projection is the easiest to ingest into something the
    operator can't always audit (a remote terminal scraper, a webhook
    relay), so we gate it tighter than the HTML / JSON siblings. Same
    contract as :mod:`app.web.routes.audit_rss`: loopback always
    accepted, remote callers must present a valid token from
    :mod:`app.feed_tokens` issued for this exact path pattern.
    """
    if not await _txt_gate_ok(request):
        client_host = request.client.host if request.client else None
        log.warning("timeline_log.txt.forbidden", client=client_host)
        raise HTTPException(
            status_code=403,
            detail="Timeline log text endpoint is loopback-only "
            "(or requires a valid ?token= for the path).",
        )

    effective_limit = _clamp_limit(limit)
    kind_filter = _normalise_kind(kind)

    items = await build_log_lines(day_iso=day, limit=effective_limit)
    filtered = _filter_lines(items, kind_filter)

    body = _format_txt(filtered)
    log.info(
        "timeline_log.txt",
        day=day or "today",
        kind=kind_filter,
        total=len(items),
        shown=len(filtered),
    )
    return PlainTextResponse(
        content=body,
        media_type="text/plain; charset=utf-8",
    )


__all__ = ["router"]
