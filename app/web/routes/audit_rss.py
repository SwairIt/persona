"""Personal RSS feed for the audit log (v0.47).

Why this exists
---------------
v0.36 gave Persona an append-only ``audit_log`` table and an HTML
viewer (``/audit``) so an operator can review *who did what* during a
security incident. That covers the reactive case — open the page after
something feels off.

v0.47 closes the proactive gap: subscribe to your own admin actions
from a feed reader, get a passive heads-up the moment anything
privileged happens. The same readers that already poll
``/feeds/journal.rss`` can poll ``/feeds/audit.rss`` and surface a
desktop notification on every new row.

Security contract
-----------------
* **Loopback-only.** The audit log is the most sensitive read in the
  whole app — it can include actor identifiers, target keys, and the
  shape of every privileged action. We refuse to serve it to anything
  that isn't ``127.0.0.1`` / ``::1``. A reverse proxy that forwards
  ``X-Forwarded-For`` is *not* trusted here — Persona is a
  single-user, local-first tool and the only legitimate consumer is a
  feed reader running on the same machine.
* **XML-safe.** Every dynamic value (action, actor, target, detail,
  timestamps) is XML-escaped before it touches the response body. The
  audit ``detail`` field is free-form caller input — even though
  :mod:`app.audit` warns callers never to put secrets there, we still
  treat it as untrusted text and escape it.
* **PASS / FAIL, not ✓ / ✗.** The task spec calls for ``[PASS]`` and
  ``[FAIL]`` rather than Unicode tick / cross glyphs. Some feed
  readers and email-based delivery pipelines mangle non-ASCII titles;
  literal text survives every encoding hop.
* **Parametrised SQL.** Mirrors :mod:`app.audit` — the table itself is
  never an injection vector here either.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from ipaddress import ip_address
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.audit import AuditRow, list_recent
from app.logging_setup import get_logger
from app.settings import get_settings

router = APIRouter(prefix="/feeds", tags=["feeds"])

log = get_logger("persona.audit.rss")

# Spec: "the last 100 audit entries". Matches the page size in
# :mod:`app.web.routes.audit` so the RSS feed and the HTML viewer agree
# on what "recent" means.
_MAX_RSS_ITEMS = 100

# SQLite stores audit ``ts`` via ``datetime('now')`` which yields
# ``'YYYY-MM-DD HH:MM:SS'`` in UTC with no timezone marker. We parse
# with this exact format so we can attach UTC explicitly and emit a
# valid RFC-822 ``pubDate``.
_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@router.get("/audit.rss")
async def audit_rss(request: Request) -> Response:
    """RSS 2.0 feed of the last 100 audit-log entries.

    Loopback-only — every non-local request is rejected with 403 before
    we touch the DB, so a misconfigured reverse proxy can't accidentally
    publish the audit trail to the open internet.
    """
    if not _is_loopback_client(request):
        client_host = request.client.host if request.client else None
        log.warning("audit.rss.forbidden", client=client_host)
        raise HTTPException(
            status_code=403,
            detail="Audit feed is loopback-only",
        )

    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    rows = await list_recent(limit=_MAX_RSS_ITEMS)

    items_xml = [_render_item(row, base) for row in rows]
    joined_items = "\n".join(items_xml)
    last_build = format_datetime(datetime.now(UTC))

    self_link = xml_escape(f"{base}/feeds/audit.rss")
    page_link = xml_escape(f"{base}/audit")

    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Persona Audit Log</title>
    <link>{page_link}</link>
    <atom:link href="{self_link}" rel="self" type="application/rss+xml" />
    <description>Privileged admin actions on this Persona instance \
— most-recent first. Loopback-only.</description>
    <lastBuildDate>{last_build}</lastBuildDate>
{joined_items}
  </channel>
</rss>
"""

    log.info("audit.rss.served", items=len(items_xml))
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from ``127.0.0.1`` / ``::1``.

    We deliberately do *not* honour ``X-Forwarded-For`` here — the
    audit feed is for the operator's local feed reader, not for any
    intermediary. If you want remote access, tunnel over SSH; do not
    poke a proxy hole.
    """
    client = request.client
    if client is None:
        return False
    try:
        return ip_address(client.host).is_loopback
    except ValueError:
        return False


def _render_item(row: AuditRow, base: str) -> str:
    """Render a single ``<item>`` element for an audit row.

    Every dynamic value is XML-escaped via :func:`xml_escape`. The
    title encodes the action and target prominently so a feed reader's
    notification surface is useful at a glance.
    """
    status = "PASS" if row["success"] else "FAIL"
    action = row["action"] or "(unknown)"
    target = row["target"] or "(none)"
    actor = row["actor"] or "(unknown)"
    detail = row["detail"] or ""
    ts_raw = row["ts"]

    title = f"[{status}] {action} on {target}"

    description_parts = [
        f"ts: {ts_raw}",
        f"actor: {actor}",
        f"action: {action}",
        f"target: {target}",
    ]
    if detail:
        description_parts.append(f"detail: {detail}")
    description = "\n".join(description_parts)

    # GUID must be stable per row + globally unique within the feed.
    # ``row["id"]`` is the audit_log primary key, so this satisfies
    # both: the same id always renders the same GUID, and ids are
    # unique by construction.
    guid_url = f"{base}/audit#row-{row['id']}"
    link_url = f"{base}/audit"

    pub_date = _format_pub_date(ts_raw)

    return f"""    <item>
      <title>{xml_escape(title)}</title>
      <link>{xml_escape(link_url)}</link>
      <guid isPermaLink="false">{xml_escape(guid_url)}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>{xml_escape(description)}</description>
    </item>"""


def _format_pub_date(ts_raw: str) -> str:
    """Format an audit ``ts`` string as an RFC-822 ``pubDate``.

    SQLite's ``datetime('now')`` yields ``'YYYY-MM-DD HH:MM:SS'`` in
    UTC with no zone marker. We parse with that exact shape and attach
    UTC explicitly so the resulting RFC-822 string is unambiguous. A
    malformed row (should never happen, but we are defensive about the
    feed because it's user-visible) falls back to *now* so the feed
    still validates rather than emitting an empty ``pubDate``.
    """
    try:
        parsed = datetime.strptime(ts_raw, _SQLITE_TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        log.warning("audit.rss.bad_ts", ts=ts_raw)
        parsed = datetime.now(UTC)
    return format_datetime(parsed)


__all__ = ["router"]
