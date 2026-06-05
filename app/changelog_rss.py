"""RSS 2.0 builder for ``/feeds/changelog.rss`` — subscribe to recent commits.

Why this exists
---------------
Persona ships a human-facing ``/changelog`` HTML page (see
:mod:`app.web.routes.changelog`) that lists the last 200 commits from
``git log`` with conventional-commit ``kind`` filters. v1.57 adds a
*passive* surface — "tell me when a new commit lands" — so a feed
reader on the operator's box can surface every shipped change without
the operator having to reload the page.

Design contract
---------------
* **Reuses :func:`app.changelog.build_changelog`.** The HTML page and
  the RSS feed share the exact same git-log source, the same 60-second
  cache, and the same ``ChangelogEntry`` shape. No second subprocess
  fan-out, no parallel parser to keep in sync.
* **Most-recent first.** ``git log`` already emits newest-first; we
  preserve that ordering for the channel items so a reader's "unread"
  view lines up with the operator's intuition of "latest change first".
* **GitHub commit URL for ``<link>``.** The HTML page wraps each sha in
  ``https://github.com/SwairIt/persona/commit/{sha}`` — the RSS feed
  mirrors that so clicking through from a reader lands on the same
  GitHub diff the page would show.
* **``guid`` = sha, ``isPermaLink=false``.** The sha is the canonical
  identity of a commit. Marking it ``isPermaLink=false`` keeps feed
  readers from re-deriving a URL from the guid — they should use the
  explicit ``<link>`` element instead.
* **``pubDate`` = ISO → RFC-822.** ``git log %ai`` emits ISO-8601 with
  a timezone offset; RSS 2.0 requires RFC-822. We parse the ISO string
  and round-trip it through :func:`email.utils.format_datetime` so the
  emitted value is spec-compliant.
* **``description`` = subject + author.** A single human-readable line
  per the spec — "<subject> — <author>" — so a reader's preview pane
  shows enough context to decide whether to click through without
  opening the commit.
* **XML-safe.** Every dynamic field (subject, author, sha, URLs) goes
  through :func:`xml.sax.saxutils.escape` before it touches the
  response body. Commit subjects are operator-supplied and can contain
  ``&`` or ``<`` legitimately (e.g. ``feat: <select> dropdown``); we
  treat them as untrusted free-form input.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Final
from xml.sax.saxutils import escape as xml_escape

from app.changelog import ChangelogEntry, build_changelog
from app.logging_setup import get_logger

log = get_logger("persona.changelog_rss")


# GitHub project the working tree maps to. Hard-coded mirror of the
# HTML template at ``app/web/templates/changelog.html`` — keep the two
# values in lockstep so the feed and the page link to the same commit
# page. If the project is ever forked / renamed, both surfaces need
# updating together.
_GITHUB_REPO_URL: Final[str] = "https://github.com/SwairIt/persona"


async def build_changelog_rss(
    host: str,
    limit: int = 100,
    *,
    kind: str | None = None,
) -> str:
    """Render the RSS 2.0 document for the most-recent commits.

    Args:
        host: Origin to prefix self-link / channel-link with — typically
            ``http://{settings.host}:{settings.port}``. A trailing slash
            is tolerated and stripped.
        limit: Maximum number of commits to include. The HTTP wrapper
            passes ``100`` per the spec; smaller values exercise the
            empty / single-item paths in tests.
        kind: Optional conventional-commit bucket filter (e.g. ``"feat"``,
            ``"fix"``). When supplied, only entries whose
            :attr:`ChangelogEntry.kind` equals the value are emitted.
            ``None`` means "no filter" — same semantics as
            ``/changelog?kind=...``.

    Returns:
        A complete RSS 2.0 XML document as a string, ready to be sent
        with ``Content-Type: application/rss+xml; charset=utf-8``.

    Raises:
        app.changelog.GitUnavailableError: When the ``git`` binary is
            not on PATH or the process is running outside a working
            tree. The route layer catches this and surfaces a 404 so
            a feed reader gets a clean "feed gone" rather than a 500.
    """
    base = host.rstrip("/")

    entries = await build_changelog(limit=limit)
    visible: list[ChangelogEntry] = (
        [e for e in entries if e["kind"] == kind] if kind is not None else entries
    )

    items_xml = [_render_item(entry) for entry in visible]
    joined_items = "\n".join(items_xml)
    last_build = format_datetime(datetime.now(UTC))

    # Round-trip the optional ``kind`` filter into the self-link and the
    # human-facing channel link so a feed reader's "open in browser"
    # action lands on the same filtered view it's subscribed to.
    kind_suffix = f"?kind={kind}" if kind else ""
    self_link = xml_escape(f"{base}/feeds/changelog.rss{kind_suffix}")
    page_link = xml_escape(f"{base}/changelog{kind_suffix}")

    if kind:
        title = f"Persona Changelog ({xml_escape(kind)})"
        description = (
            "Auto-generated changelog filtered by kind="
            f"{xml_escape(kind)} — newest commits first."
        )
    else:
        title = "Persona Changelog"
        description = (
            "Auto-generated changelog derived from this Persona instance's "
            "git history — newest commits first."
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{title}</title>\n"
        f"    <link>{page_link}</link>\n"
        f'    <atom:link href="{self_link}" rel="self" '
        'type="application/rss+xml" />\n'
        f"    <description>{description}</description>\n"
        f"    <lastBuildDate>{last_build}</lastBuildDate>\n"
        f"{joined_items}\n"
        "  </channel>\n"
        "</rss>\n"
    )

    log.info(
        "changelog_rss.built",
        items=len(items_xml),
        bytes=len(body),
        kind=kind,
    )
    return body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_item(entry: ChangelogEntry) -> str:
    """Render a single ``<item>`` element for one parsed commit row.

    Every dynamic field is XML-escaped at the point of emission — the
    commit subject and author are operator-supplied free-form strings
    and can legitimately contain ``&`` / ``<`` / ``>``.
    """
    sha = entry["sha"]
    subject = entry["subject"]
    author = entry["author"]
    date_iso = entry["date_iso"]

    pub_date = _format_pubdate(date_iso)
    link_url = f"{_GITHUB_REPO_URL}/commit/{sha}"
    description = f"{subject} — {author}" if author else subject

    return (
        "    <item>\n"
        f"      <title>{xml_escape(subject)}</title>\n"
        f"      <link>{xml_escape(link_url)}</link>\n"
        f'      <guid isPermaLink="false">{xml_escape(sha)}</guid>\n'
        f"      <pubDate>{pub_date}</pubDate>\n"
        f"      <description>{xml_escape(description)}</description>\n"
        "    </item>"
    )


def _format_pubdate(date_iso: str) -> str:
    """Convert ``git log %ai`` ISO-8601 to an RFC-822 ``<pubDate>``.

    ``%ai`` emits ``"YYYY-MM-DD HH:MM:SS +ZZZZ"`` — a space between
    date and time rather than the ``T`` :func:`datetime.fromisoformat`
    accepts on older Pythons, and a space before the tz offset. We
    normalise both before parsing. A truly malformed value falls back
    to *now* in UTC so the feed still validates rather than raising
    mid-render — the same defensive posture the pinned feed takes.
    """
    candidate = date_iso.strip()
    if not candidate:
        log.warning("changelog_rss.bad_ts", ts=date_iso)
        return format_datetime(datetime.now(UTC))

    # ``git log %ai`` → ``"2026-06-05 12:34:56 +0300"``.
    # :func:`datetime.fromisoformat` on 3.11+ accepts the space
    # separator but not the space before the offset, so we strip the
    # offset-space when present.
    normalised = candidate
    # Find a tz-offset token like " +0300" / " -0500" / " +03:00" and
    # collapse the preceding space so ``fromisoformat`` accepts it.
    for marker in (" +", " -"):
        idx = normalised.rfind(marker)
        # Guard against a stray ``-`` inside the date portion: the
        # offset must appear after the time component, i.e. past the
        # first ``:`` of HH:MM:SS.
        first_colon = normalised.find(":")
        if idx > first_colon >= 0:
            normalised = normalised[:idx] + normalised[idx + 1 :]
            break

    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        log.warning("changelog_rss.bad_ts", ts=date_iso)
        return format_datetime(datetime.now(UTC))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return format_datetime(parsed)


__all__ = ["build_changelog_rss"]
