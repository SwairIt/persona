"""OPML 2.0 export of every RSS / Atom feed Persona exposes.

Why this exists
---------------
Persona's RSS surface has grown organically — :mod:`app.web.routes.rss`
ships ``/feeds/journal.rss``, ``/feeds/digest/weekly.rss``,
``/feeds/annotations.rss`` plus the per-tag, per-collection and per
saved-search families, and :mod:`app.web.routes.audit_rss` adds the
loopback-only ``/feeds/audit.rss``. A power user running Feedly,
NetNewsWire or Inoreader wants to subscribe to *all* of them in one
go rather than copy-pasting each URL.

OPML 2.0 is the standard import format every mainstream reader
understands. :func:`build_opml` renders a single XML document that
lists every canonical feed Persona offers; the HTTP wrapper lives in
:mod:`app.web.routes.opml_export`.

Design contract
---------------
* **Hardcoded canonical list.** Only the always-on feeds are listed —
  per-tag, per-collection and per-saved-search families need DB lookups
  and are out of scope here (the existing ``/rss`` discovery page
  already covers that). One entry points readers at the per-collection
  *index* page so they can pick the slugs they care about.
* **Absolute URLs.** OPML readers don't know the originating host;
  every ``xmlUrl`` / ``htmlUrl`` is prefixed with the supplied
  ``host`` so a reader can subscribe without further rewriting.
* **Optional token gating.** When the caller passes ``token=…`` we
  append ``?token=…`` to every feed URL — Persona's feed-auth path
  matches the token against the request path with ``fnmatch``, so a
  ``/feeds/*`` pattern covers the whole OPML bundle in one go.
* **XML-safe.** Title and description fields go through
  :func:`xml.sax.saxutils.escape` plus the ``{'"': '&quot;'}`` quote
  map so they survive embedding inside double-quoted XML attributes.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote as _url_quote
from xml.sax.saxutils import escape as _xml_escape

from app.logging_setup import get_logger

log = get_logger("persona.opml_export")


# Attribute-safe XML escape: the stdlib ``escape`` only handles ``<``,
# ``>`` and ``&`` by default; we extend it with the quote map so values
# can sit inside double-quoted attributes without breaking the parser.
_ATTR_ESCAPE_MAP: dict[str, str] = {'"': "&quot;", "'": "&apos;"}


def _attr(value: str) -> str:
    """Return ``value`` escaped for use inside an XML attribute."""
    return _xml_escape(value, _ATTR_ESCAPE_MAP)


@dataclass(frozen=True)
class _FeedEntry:
    """One canonical Persona feed listed in the OPML bundle.

    Frozen so the module-level list is safely shared across requests.
    """

    title: str
    relative_url: str
    description: str


# Canonical always-on feeds. Sources verified against
# :mod:`app.web.routes.rss` and :mod:`app.web.routes.audit_rss` — the
# only ``.rss`` endpoints with no path parameter. The per-collection
# index entry points at the HTML discovery page so a reader can pick
# the per-tag / per-collection / per-saved-search feeds they want.
_CANONICAL_FEEDS: tuple[_FeedEntry, ...] = (
    _FeedEntry(
        title="Persona — Journal",
        relative_url="/feeds/journal.rss",
        description="Most-recent screenshot notes (up to 200).",
    ),
    _FeedEntry(
        title="Persona — Audit log",
        relative_url="/feeds/audit.rss",
        description=(
            "Last 100 privileged actions on this instance "
            "(loopback-only — only readers running on this machine can poll it)."
        ),
    ),
    _FeedEntry(
        title="Persona — Weekly digests",
        relative_url="/feeds/digest/weekly.rss",
        description="Most-recent weekly LLM retrospectives, newest first.",
    ),
    _FeedEntry(
        title="Persona — Annotations",
        relative_url="/feeds/annotations.rss",
        description="Most-recent margin scribbles across all screenshots.",
    ),
    _FeedEntry(
        title="Persona — Per-collection feeds index",
        relative_url="/rss",
        description=(
            "Discovery page listing every per-tag, per-auto-collection and "
            "per-saved-search RSS feed exposed by this instance — pick the "
            "slugs you want and copy each URL into your reader."
        ),
    ),
)


def _normalise_host(host: str) -> str:
    """Strip any trailing slash so we can safely concatenate paths.

    A caller may legitimately pass either ``http://127.0.0.1:8765`` or
    ``http://127.0.0.1:8765/`` (FastAPI's ``request.base_url`` always
    yields the latter); both must produce the same OPML output.
    """
    return host.rstrip("/")


def _absolute_url(host: str, relative: str, token: str | None) -> str:
    """Compose an absolute feed URL, appending ``?token=…`` when given.

    ``token`` is URL-quoted with an empty safe set so any operator-issued
    value (which is base64-urlsafe in :mod:`app.feed_tokens` but
    treated as opaque here) round-trips intact even if a future
    revision introduces special characters.
    """
    base = _normalise_host(host) + relative
    if not token:
        return base
    return f"{base}?token={_url_quote(token, safe='')}"


def build_opml(
    host: str = "http://127.0.0.1:8765",
    token: str | None = None,
) -> str:
    """Render the OPML 2.0 document listing every canonical Persona feed.

    Args:
        host: Origin to prefix every relative feed URL with — typically
            derived from ``request.base_url`` in the HTTP wrapper. A
            trailing slash is tolerated and stripped.
        token: Optional feed-auth token. When supplied it is appended
            as ``?token=…`` to every feed URL so a single OPML import
            covers token-gated subscriptions in one go.

    Returns:
        A complete OPML 2.0 XML document as a string, ready to be sent
        with ``Content-Type: text/x-opml; charset=utf-8``.
    """
    normalised_host = _normalise_host(host)
    token_present = bool(token)

    outlines: list[str] = []
    for entry in _CANONICAL_FEEDS:
        feed_url = _absolute_url(normalised_host, entry.relative_url, token)
        # ``htmlUrl`` points at the host root so the reader has a sane
        # fall-back if it surfaces a "site home" link in the UI.
        outlines.append(
            "    <outline "
            f'type="rss" '
            f'text="{_attr(entry.title)}" '
            f'title="{_attr(entry.title)}" '
            f'description="{_attr(entry.description)}" '
            f'xmlUrl="{_attr(feed_url)}" '
            f'htmlUrl="{_attr(normalised_host + "/")}"'
            " />"
        )

    body_outlines = "\n".join(outlines)
    title = _xml_escape("Persona — all RSS feeds")
    head_block = (
        "  <head>\n"
        f"    <title>{title}</title>\n"
        "  </head>\n"
    )
    body_block = (
        "  <body>\n"
        f"{body_outlines}\n"
        "  </body>\n"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<opml version="2.0">\n'
        f"{head_block}"
        f"{body_block}"
        "</opml>\n"
    )

    log.info(
        "opml_export.built",
        host=normalised_host,
        feeds=len(_CANONICAL_FEEDS),
        token_present=token_present,
        bytes=len(document),
    )
    return document


__all__ = ["build_opml"]
