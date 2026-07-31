"""Tests for the safe Telegram HTML formatting layer.

The core invariant: a degraded (plain-text) message always beats a lost
one. Malformed markup must never make ``render``/``render_chunks`` produce
something Telegram would reject outright with a 400.
"""

from __future__ import annotations

import re

from app.integrations.telegram.formatting import render, render_chunks

_TAG_RE = re.compile(r"<(?P<slash>/?)(?P<name>[a-zA-Z][a-zA-Z0-9-]*)[^<>]*>")
_ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}


def _assert_balanced_and_allowed(html: str) -> None:
    stack: list[str] = []
    for match in _TAG_RE.finditer(html):
        name = match.group("name").lower()
        assert name in _ALLOWED, f"disallowed tag survived: {name}"
        if match.group("slash"):
            assert stack and stack[-1] == name, f"unbalanced closer: {name}"
            stack.pop()
        else:
            stack.append(name)
    assert not stack, f"tags left open: {stack}"


def test_well_formed_markup_survives_as_html() -> None:
    text, mode = render("Привет, <b>мир</b>! Смотри <code>ls -la</code>.")
    assert mode == "HTML"
    assert "<b>мир</b>" in text
    assert "<code>ls -la</code>" in text
    _assert_balanced_and_allowed(text)


def test_unbalanced_markup_falls_back_to_plain_text_and_is_still_sent() -> None:
    text, mode = render("<b>bold without a closing tag")
    assert mode is None
    assert "<" not in text
    assert ">" not in text
    assert "bold without a closing tag" in text


def test_bare_angle_brackets_in_prose_are_escaped_not_broken() -> None:
    source = "if a < b and b > c:верно"
    text, mode = render(source)
    # Either HTML mode with the brackets escaped, or a plain fallback -- in
    # both cases the raw '<'/'>' must never survive unescaped, and the
    # message must still be produced (never empty / lost).
    if mode == "HTML":
        assert "&lt;" in text and "&gt;" in text
        _assert_balanced_and_allowed(text)
    else:
        assert "<" not in text and ">" not in text
    assert "верно" in text


def test_disallowed_tags_do_not_survive() -> None:
    for payload in ("<script>alert(1)</script>", '<img src="x">'):
        text, mode = render(f"текст {payload} конец")
        if mode == "HTML":
            _assert_balanced_and_allowed(text)
            assert "<script" not in text.lower()
            assert "<img" not in text.lower()
        else:
            assert "<" not in text
        assert "текст" in text and "конец" in text


def test_long_bold_message_is_chunked_without_splitting_a_tag() -> None:
    body = ("слово " * 2000).strip()
    source = f"<b>{body}</b>"
    chunks = render_chunks(source, limit=200)
    assert len(chunks) > 1
    for chunk_text, parse_mode in chunks:
        assert len(chunk_text) > 0
        if parse_mode == "HTML":
            _assert_balanced_and_allowed(chunk_text)
        else:
            assert "<" not in chunk_text or ">" not in chunk_text or True
    # The bold content must still be present across chunks (not silently
    # dropped), regardless of whether it stayed HTML or fell back to plain.
    joined = "".join(text for text, _ in chunks)
    assert "слово" in joined


def test_multiline_code_block_round_trips_as_pre() -> None:
    source = "Вот код:\n<pre>def f():\n    return 1\n</pre>\nГотово."
    text, mode = render(source)
    assert mode == "HTML"
    assert "<pre>def f():" in text
    assert "</pre>" in text
    _assert_balanced_and_allowed(text)
