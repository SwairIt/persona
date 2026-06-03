"""Scan standalone notes for URLs and probe each one over HTTP.

v0.77 ships a small admin utility: walk the ``notes`` table, pull every
``https?://...`` URL out of the body text, and record which ones still
respond with a 2xx. Anything else — DNS failure, timeout, 3xx redirect
chain that lands on an error, plain old 404 — is reported back so the
operator can clean up rot in their own personal knowledge base.

Design constraints
------------------
* **Async I/O only.** :class:`httpx.AsyncClient` is reused across every
  URL in a single run so connection pooling can amortise the TLS
  handshake when the same domain appears on multiple notes.
* **HEAD-first, GET-fallback.** Most well-behaved servers answer HEAD
  cheaply; some (GitHub raw, S3 presigned URLs, CDNs that route HEAD to
  a different worker) reply with ``405 Method Not Allowed`` or
  ``501 Not Implemented`` instead. We retry those once with GET and use
  ``follow_redirects=True`` so a 301-to-200 chain reports as 200.
* **Encrypted notes are skipped.** The ``notes.encrypted`` column flags
  rows whose ``body`` is ciphertext — scanning that with ``re.findall``
  would either return junk or, worse, the literal ciphertext bytes. We
  honour the encryption boundary instead.
* **Hard caps.** ``max_links`` clamps the total number of HTTP requests
  per call so a runaway note (a copy-pasted log full of URLs) can't
  hold the worker for minutes. The cap is enforced *after* extraction —
  the rest of the URLs are simply not probed; the caller sees a shorter
  result list and can re-run if they care.
* **Per-URL try/except.** One bad URL (malformed host, SSL error) must
  never abort the rest of the run. Failures are recorded with
  ``status = 0`` and the error message in ``error``.

The function is consumed by :mod:`app.web.routes.notes_link_checker`,
which persists the JSON result blob to ``kv_settings`` and renders it
in the admin UI. It is intentionally side-effect-free here — writing
the result to the DB is the route's responsibility.
"""

from __future__ import annotations

import re
from typing import Final, TypedDict

import httpx

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.link_checker")


# Greedy enough to pick up the common cases (``https://example.com/foo``,
# ``http://x.test/path?q=1#frag``) without dragging in trailing
# punctuation that almost always belongs to the surrounding prose
# (a closing paren, a comma, a sentence-ending period). The trim happens
# in :func:`_strip_trailing_punct` so the regex itself stays simple.
_URL_RE: Final = re.compile(r"https?://\S+")

# Punctuation that almost always belongs to the surrounding sentence
# rather than the URL itself. Stripped from the *right* edge only —
# leading punctuation inside a URL is exotic enough we'd rather flag a
# false-negative than mangle a legitimate fragment.
_TRAILING_PUNCT: Final[str] = ".,);]>'\"!?"

# Default request budget per :func:`check_all_links` call.
# 200 HTTP round-trips at ``timeout=5`` is roughly 5-30s of wall-clock
# under normal latency — fast enough to run synchronously from a POST
# handler, slow enough that we don't want to remove the cap.
_DEFAULT_MAX_LINKS: Final[int] = 200

# Status sentinel used when the request itself never produced an HTTP
# response (DNS failure, connection refused, TLS error, timeout). Using
# ``0`` keeps the result row a uniform ``int`` instead of ``int | None``,
# which the template's "bad row" filter can treat as "not 2xx" without
# special-casing ``None``.
_STATUS_TRANSPORT_ERROR: Final[int] = 0

# Cap on the per-URL error string so a multi-line SSL traceback can't
# bloat the JSON blob we stash in ``kv_settings``.
_ERROR_TRUNCATE: Final[int] = 200


class LinkCheckResult(TypedDict):
    """One row of :func:`check_all_links`'s return value.

    ``status`` is the HTTP status code on a successful round-trip and
    :data:`_STATUS_TRANSPORT_ERROR` (``0``) on a transport-level failure;
    callers should treat anything outside ``200..299`` as bad. ``error``
    is only populated on transport failures — it stays at ``None`` for
    a real HTTP response, even a 404.
    """

    note_id: int
    url: str
    status: int
    error: str | None


def _strip_trailing_punct(url: str) -> str:
    """Trim sentence-tail punctuation from the right edge of a URL.

    Plain notes are markdown-ish: ``"see https://example.com/foo."`` is
    a sentence, not a URL with a literal trailing period. We walk
    backwards from the end and drop any character that appears in
    :data:`_TRAILING_PUNCT`. This is a heuristic — a legitimate URL with
    a path component ending in ``)`` would lose it — but the alternative
    is reporting hundreds of spurious 404s, which is worse.
    """
    end = len(url)
    while end > 0 and url[end - 1] in _TRAILING_PUNCT:
        end -= 1
    return url[:end]


def _extract_urls(body: str) -> list[str]:
    """Return every distinct ``http(s)://...`` URL in ``body``.

    Dedups while preserving first-seen order so the result table reads
    top-to-bottom like the note itself. The dedup happens *after*
    :func:`_strip_trailing_punct` so ``"https://x.test."`` and
    ``"https://x.test"`` collapse into a single probe.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in _URL_RE.findall(body):
        url = _strip_trailing_punct(raw)
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


async def _load_note_urls(max_links: int) -> list[tuple[int, str]]:
    """Read every plaintext note and yield ``(note_id, url)`` pairs.

    Encrypted rows are skipped — their ``body`` column is ciphertext and
    matching ``\\S+`` against it would either find nothing or, on a
    base64'd blob, hallucinate a non-URL. The flag is set by the
    encryption pipeline in :mod:`app.encrypted_notes`.

    The output is truncated to ``max_links`` so the caller's HTTP loop
    never exceeds its budget; we still iterate every note (the SQL scan
    is cheap) so the truncation is observable in the log line below.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, body
              FROM notes
             WHERE COALESCE(encrypted, 0) = 0
             ORDER BY id DESC
            """,
        )
        rows = list(await cursor.fetchall())

    pairs: list[tuple[int, str]] = []
    for row in rows:
        body = row["body"]
        if body is None:
            continue
        for url in _extract_urls(str(body)):
            pairs.append((int(row["id"]), url))
            if len(pairs) >= max_links:
                log.info(
                    "link_checker.extracted.capped",
                    cap=max_links,
                    note_count=len(rows),
                )
                return pairs

    log.info(
        "link_checker.extracted",
        pair_count=len(pairs),
        note_count=len(rows),
    )
    return pairs


async def _probe_one(
    client: httpx.AsyncClient,
    url: str,
) -> tuple[int, str | None]:
    """Issue a HEAD (with GET fallback on 405/501) and return ``(status, err)``.

    ``follow_redirects=True`` is set on the client itself so a chain of
    301s lands on its final destination before we record the status —
    that way ``"http://example.com"`` → ``"https://example.com/"`` → 200
    shows up as a green row instead of a misleading 301.

    Any transport-level failure (timeout, DNS, TLS) is returned as
    ``(0, error_message)`` so the caller can write a uniform row. We
    deliberately don't re-raise — one broken URL must not stop the run.
    """
    try:
        response = await client.head(url)
    except (httpx.HTTPError, TimeoutError) as exc:
        return _STATUS_TRANSPORT_ERROR, str(exc)[:_ERROR_TRUNCATE]

    status = int(response.status_code)
    # 405 Method Not Allowed and 501 Not Implemented both signal "this
    # server understands HTTP but not HEAD" — retry once with GET. We
    # don't fall through on every non-2xx because a real 404 from HEAD
    # is the same answer GET would give, just cheaper.
    if status in {405, 501}:
        try:
            response = await client.get(url)
        except (httpx.HTTPError, TimeoutError) as exc:
            return _STATUS_TRANSPORT_ERROR, str(exc)[:_ERROR_TRUNCATE]
        status = int(response.status_code)

    return status, None


async def check_all_links(
    *,
    timeout: float = 5.0,
    max_links: int = _DEFAULT_MAX_LINKS,
) -> list[LinkCheckResult]:
    """Probe every URL in every plaintext note and report the outcome.

    :param timeout: Per-request socket timeout in seconds. Applied
        uniformly across connect / read / write / pool — a fast 5s
        ceiling is the default because the page render waits on this
        call and we'd rather report a transport error than spin.
    :param max_links: Hard ceiling on the number of HTTP requests this
        call will issue. URLs beyond the cap are silently dropped from
        the result; the log line above records the truncation. Set to a
        larger value when manually invoked for a one-off audit.

    Returns one :class:`LinkCheckResult` per probed URL. Multiple notes
    can contribute the same URL, in which case it appears once per
    ``(note_id, url)`` pair — the caller (admin page) shows the
    note-by-note view, so we keep duplicates across notes intentionally.
    """
    if max_links <= 0:
        return []

    pairs = await _load_note_urls(max_links)
    if not pairs:
        log.info("link_checker.empty")
        return []

    results: list[LinkCheckResult] = []
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        for note_id, url in pairs:
            status, error = await _probe_one(client, url)
            results.append(
                LinkCheckResult(
                    note_id=note_id,
                    url=url,
                    status=status,
                    error=error,
                )
            )

    bad = sum(1 for r in results if not 200 <= r["status"] < 300)
    log.info(
        "link_checker.done",
        total=len(results),
        bad=bad,
        timeout=timeout,
        max_links=max_links,
    )
    return results


__all__ = [
    "LinkCheckResult",
    "check_all_links",
]
