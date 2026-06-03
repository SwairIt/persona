"""Render ``CHANGELOG.md`` as a friendly versioned timeline at ``/whats-new``.

v1.0 capstone feature 2/3. Reads the project-root ``CHANGELOG.md`` on every
request (it's a single small file — re-reading on demand keeps the page
fresh after a release without a restart) and parses it with a tiny stdlib
state machine: ``## heading`` opens a new version card, everything until
the next ``##`` lands in that card's body. The body is rendered as a list
of bullet items (lines beginning with ``-`` or ``*``) plus free-form
paragraphs so even a sparse changelog still looks like a timeline.

Pure stdlib — no ``markdown`` / ``mistune`` dependency. The template
escapes everything, so the raw text is safe to pass through. If the file
is missing the page still renders with a friendly placeholder rather than
500-ing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import __version__
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["whats-new"])

log = get_logger("persona.whats_new")

# ``Path(__file__).parents`` walks:
#   [0] -> app/web/routes
#   [1] -> app/web
#   [2] -> app
#   [3] -> <project root>            <-- CHANGELOG.md lives here
# Resolved once at import time; the path is process-stable.
_CHANGELOG_PATH: Path = Path(__file__).resolve().parents[3] / "CHANGELOG.md"

# Hard cap on the file size we'll read. CHANGELOG.md is hand-written
# release notes — 512 KiB is already a decade of features. The cap stops
# a future operator-edit from accidentally hanging the request with a
# multi-megabyte blob.
_MAX_CHANGELOG_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class ChangelogEntry:
    """One ``## heading`` block parsed out of ``CHANGELOG.md``.

    ``heading`` is the raw text after ``## `` (e.g. ``"v1.0 — 2026-06-03"``).
    ``bullets`` are list items (``-`` / ``*`` prefixes, dash stripped).
    ``paragraphs`` are free-form non-bullet lines, blank-line separated.
    """

    heading: str
    bullets: tuple[str, ...]
    paragraphs: tuple[str, ...]


def _parse_changelog(text: str) -> list[ChangelogEntry]:
    """Split ``text`` into one :class:`ChangelogEntry` per ``## heading``.

    The parser is intentionally tolerant — a CHANGELOG written by humans
    will have stray blank lines, inconsistent bullet markers, and the
    occasional ``###`` sub-heading. We treat any line that starts with
    exactly ``## `` (two hashes + space) as a new version, everything
    until the next such line as that version's body, and ignore the
    leading material before the first ``##`` (typically the ``# Changelog``
    title plus an intro paragraph).
    """
    entries: list[ChangelogEntry] = []
    current_heading: str | None = None
    current_bullets: list[str] = []
    current_paragraph_lines: list[str] = []
    current_paragraphs: list[str] = []

    def _flush_paragraph() -> None:
        if current_paragraph_lines:
            joined = " ".join(line.strip() for line in current_paragraph_lines).strip()
            if joined:
                current_paragraphs.append(joined)
            current_paragraph_lines.clear()

    def _flush_entry() -> None:
        nonlocal current_heading, current_bullets, current_paragraphs
        _flush_paragraph()
        if current_heading is not None:
            entries.append(
                ChangelogEntry(
                    heading=current_heading,
                    bullets=tuple(current_bullets),
                    paragraphs=tuple(current_paragraphs),
                )
            )
        current_heading = None
        current_bullets = []
        current_paragraphs = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## ") and not line.startswith("### "):
            _flush_entry()
            current_heading = line[3:].strip()
            continue
        if current_heading is None:
            # Pre-amble before the first version — ignore.
            continue
        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ")):
            _flush_paragraph()
            current_bullets.append(stripped[2:].strip())
            continue
        if not stripped:
            _flush_paragraph()
            continue
        # Skip subsection headers (### / ####) but keep their text as a
        # paragraph so context isn't lost.
        if stripped.startswith("#"):
            _flush_paragraph()
            current_paragraphs.append(stripped.lstrip("#").strip())
            continue
        current_paragraph_lines.append(stripped)

    _flush_entry()
    return entries


def _read_changelog() -> tuple[str | None, list[ChangelogEntry]]:
    """Return ``(error_message, entries)`` for the template.

    ``error_message`` is non-``None`` only when something went wrong:
    the file is missing, unreadable, or larger than the safety cap. The
    template renders the message in a soft callout instead of the
    timeline. ``entries`` is always a list (possibly empty) so the
    template never needs ``is not None`` checks.
    """
    try:
        stat = _CHANGELOG_PATH.stat()
    except FileNotFoundError:
        log.info("whats_new.changelog.missing", path=str(_CHANGELOG_PATH))
        return ("CHANGELOG.md not found in the project root yet.", [])
    except OSError as exc:
        log.warning(
            "whats_new.changelog.stat_failed",
            path=str(_CHANGELOG_PATH),
            error=str(exc),
        )
        return (f"Could not read CHANGELOG.md: {exc}", [])

    if stat.st_size > _MAX_CHANGELOG_BYTES:
        log.warning(
            "whats_new.changelog.too_large",
            path=str(_CHANGELOG_PATH),
            size=stat.st_size,
            cap=_MAX_CHANGELOG_BYTES,
        )
        return (
            f"CHANGELOG.md is {stat.st_size} bytes, refusing to render "
            f"(cap is {_MAX_CHANGELOG_BYTES}).",
            [],
        )

    try:
        text = _CHANGELOG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "whats_new.changelog.read_failed",
            path=str(_CHANGELOG_PATH),
            error=str(exc),
        )
        return (f"Could not read CHANGELOG.md: {exc}", [])
    except UnicodeDecodeError as exc:
        log.warning(
            "whats_new.changelog.decode_failed",
            path=str(_CHANGELOG_PATH),
            error=str(exc),
        )
        return ("CHANGELOG.md is not valid UTF-8.", [])

    entries = _parse_changelog(text)
    log.info(
        "whats_new.changelog.parsed",
        path=str(_CHANGELOG_PATH),
        size=stat.st_size,
        entries=len(entries),
    )
    return (None, entries)


@router.get("/whats-new", response_class=HTMLResponse)
async def whats_new_page(request: Request) -> HTMLResponse:
    """Render the parsed ``CHANGELOG.md`` as a versioned timeline."""
    error, entries = _read_changelog()
    return templates.TemplateResponse(
        request,
        "whats_new.html",
        {
            "title": "What's new",
            "active_nav": "settings",
            "version": __version__,
            "entries": entries,
            "error": error,
        },
    )
