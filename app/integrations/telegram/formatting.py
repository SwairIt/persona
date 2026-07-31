"""Safe Telegram HTML formatting for outgoing messages.

Telegram's HTML ``parse_mode`` parser is strict: one unclosed tag or one
stray ``<`` rejects the ENTIRE message with an HTTP 400 -- the chat gets
nothing at all, not a degraded plain-text version. A small local model will
occasionally emit malformed markup, so this module treats "produce HTML" as
a best-effort step that always has a safe, guaranteed-valid fallback.

Usage: ``render(raw_text) -> (text, parse_mode)``. ``parse_mode`` is either
``"HTML"`` (validated, safe to send as-is) or ``None`` (plain text, all
markup stripped -- always sendable).
"""

from __future__ import annotations

import re

# Tags Persona is allowed to use, exactly as Telegram's Bot API HTML mode
# supports them. Anything else gets escaped away, never rendered as a tag.
_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}

# Matches a well-formed HTML-ish tag: ``<tag ...>``, ``</tag>``. Anything
# that does not match this shape (e.g. a bare ``<`` or ``a < b``) is left to
# the escaping pass, which turns it into ``&lt;``.
_TAG_RE = re.compile(
    r"<(?P<slash>/?)(?P<name>[a-zA-Z][a-zA-Z0-9-]*)(?P<attrs>[^<>]*)>"
)

# href="..." or href='...' -- the only attribute allowed, and only on <a>.
_HREF_RE = re.compile(r"""href\s*=\s*(?P<q>["'])(?P<url>.*?)(?P=q)""", re.IGNORECASE)


def render(raw_text: str) -> tuple[str, str | None]:
    """Return ``(text, parse_mode)`` safe to hand straight to sendMessage.

    Tries to keep Persona's allowed HTML tags; falls back to a fully
    plain-text rendering (markup stripped, everything escaped away) if the
    HTML version does not validate. The fallback can never itself be
    malformed, so this function always returns something sendable.
    """
    source = str(raw_text or "")
    html = _build_html(source)
    if _is_valid(html):
        return html, "HTML"
    return strip_formatting(source), None


def strip_formatting(raw_text: str) -> str:
    """Remove every recognised tag, keeping only their inner text.

    Used both as the guaranteed-safe fallback and for the pre-flight
    plain-text retry if Telegram itself rejects the HTML version.
    """
    text = str(raw_text or "")

    def _drop_tag(match: re.Match[str]) -> str:
        return ""

    # Repeatedly strip tags (not entities) so no literal "<...>" survives,
    # then unescape nothing else -- the source was never HTML-escaped.
    return _TAG_RE.sub(_drop_tag, text)


def _build_html(source: str) -> str:
    """Escape everything, then re-open only well-formed, allowed tags.

    Strategy: walk the source tag-by-tag. Anything matching ``_TAG_RE`` and
    naming an allowed tag is emitted as a real tag (with its ``href``
    sanitised, for ``<a>``); everything else -- including malformed-looking
    ``<`` characters, disallowed tags, and ordinary text -- is HTML-escaped.
    """
    out: list[str] = []
    pos = 0
    for match in _TAG_RE.finditer(source):
        out.append(_escape_text(source[pos : match.start()]))
        name = match.group("name").lower()
        if name in _ALLOWED_TAGS:
            if match.group("slash"):
                out.append(f"</{name}>")
            elif name == "a":
                href_match = _HREF_RE.search(match.group("attrs") or "")
                url = href_match.group("url") if href_match else ""
                out.append(f'<a href="{_escape_attr(url)}">')
            else:
                out.append(f"<{name}>")
        else:
            # Disallowed tag (e.g. <script>, <img>): escape it as text so
            # it renders literally instead of being interpreted.
            out.append(_escape_text(match.group(0)))
        pos = match.end()
    out.append(_escape_text(source[pos:]))
    return "".join(out)


def _escape_text(chunk: str) -> str:
    return chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_chunks(raw_text: str, limit: int = 3900) -> list[tuple[str, str | None]]:
    """Render ``raw_text`` and split it into Telegram-sized chunks.

    Each returned ``(text, parse_mode)`` pair is independently valid and
    sendable on its own -- a chunk boundary can never land inside a tag,
    and any tag still open at a cut point is closed at the end of that
    chunk and reopened at the start of the next one. If the source markup
    does not validate as a whole, every chunk falls back to plain text
    (matching :func:`render`'s single-message behaviour).
    """
    source = str(raw_text or "").strip() or "(пустой ответ)"
    html = _build_html(source)
    if not _is_valid(html):
        return [(chunk, None) for chunk in _split_plain(strip_formatting(source), limit)]
    if len(html) <= limit:
        return [(html, "HTML")]
    chunks = _chunk_valid_html(html, limit)
    # Defensive re-check: if tag-aware chunking ever produced something
    # invalid (it should not), fall back to plain text for the whole
    # message rather than risk sending a malformed chunk.
    if not all(_is_valid(chunk) for chunk in chunks):
        return [(chunk, None) for chunk in _split_plain(strip_formatting(source), limit)]
    return [(chunk, "HTML") for chunk in chunks]


def _split_plain(text: str, limit: int) -> list[str]:
    """Split plain text on readable boundaries, same shape as the old splitter."""
    remaining = text or "(пустой ответ)"
    chunks: list[str] = []
    while len(remaining) > limit:
        boundary = max(
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _chunk_valid_html(html: str, limit: int) -> list[str]:
    """Split already-valid, balanced HTML into independently valid chunks.

    Walks the string picking safe cut points (never inside a tag), then
    closes whatever tags are still open at each cut and reopens them at
    the start of the next chunk so every chunk balances on its own.
    """
    matches = list(_TAG_RE.finditer(html))
    length = len(html)
    chunks: list[str] = []
    start = 0
    open_stack: list[tuple[str, str]] = []  # (tag name, opening tag text)
    while start < length:
        limit_pos = min(start + limit, length)
        if limit_pos >= length:
            end = length
        else:
            end = _safe_boundary(html, start, limit_pos, matches)
        prefix = "".join(tag for _, tag in open_stack)
        stack = list(open_stack)
        for match in matches:
            if match.start() < start:
                continue
            if match.start() >= end:
                break
            name = match.group("name").lower()
            if match.group("slash"):
                if stack and stack[-1][0] == name:
                    stack.pop()
            else:
                stack.append((name, match.group(0)))
        suffix = "".join(f"</{name}>" for name, _ in reversed(stack))
        chunks.append(prefix + html[start:end] + suffix)
        open_stack = stack
        start = end
    return chunks


def _safe_boundary(
    html: str, start: int, limit_pos: int, matches: list[re.Match[str]]
) -> int:
    """Pick a cut point in ``(start, limit_pos]`` that never falls inside a tag."""
    boundary = limit_pos
    whitespace = max(
        html.rfind("\n", start, limit_pos),
        html.rfind(" ", start, limit_pos),
    )
    if whitespace > start + (limit_pos - start) // 2:
        boundary = whitespace
    for match in matches:
        if match.start() < boundary < match.end():
            boundary = match.start() if match.start() > start else match.end()
            break
    if boundary <= start:
        boundary = limit_pos
    return min(boundary, len(html))


def _is_valid(html: str) -> bool:
    """Check every tag in ``html`` is allowed, balanced, and non-overlapping.

    This is a structural sanity check on markup this module itself produced
    (it never runs on arbitrary external input), so a straightforward stack
    matcher is sufficient -- no need to reimplement Telegram's full grammar.
    """
    stack: list[str] = []
    for match in _TAG_RE.finditer(html):
        name = match.group("name").lower()
        if name not in _ALLOWED_TAGS:
            return False
        if match.group("slash"):
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
    return not stack


__all__ = ["render", "strip_formatting"]
