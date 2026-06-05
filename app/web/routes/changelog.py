"""HTML + JSON endpoints for the auto-generated changelog page.

Two endpoints live here:

* ``GET /changelog`` — the human-facing page. Renders ``changelog.html``
  extending ``base.html`` with ``active_nav="settings"`` so the
  side-nav highlights the right group.
* ``GET /api/changelog.json`` — machine-readable JSON of the same
  payload, handy for scripts that want to surface "what changed in the
  last hour" elsewhere (a Slack ping, a digest email).

Both endpoints support a ``?kind=feat`` (or any other known kind)
query-string filter. Unknown kinds collapse to "no filter" rather than
404'ing — the page is for browsing, not API correctness.

When the underlying :mod:`app.changelog` module raises
:class:`~app.changelog.GitUnavailableError` (no ``git`` on PATH, or the
process happens to be running outside a working tree) the HTML page
renders a friendly "Changelog unavailable" notice and the JSON endpoint
returns ``404`` with a structured ``detail`` field. Per the spec, both
behaviours are graceful — never a 500 from a missing binary.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.changelog import (
    ChangelogEntry,
    GitUnavailableError,
    build_changelog,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["changelog"])

log = get_logger("persona.changelog.routes")

# The kinds we expose as filter chips in the UI. Keep this in sync with
# ``_KIND_PREFIXES`` + ``_OTHER_KIND`` over in :mod:`app.changelog` —
# the tests in ``tests/test_changelog.py`` cover the contract.
_KNOWN_KINDS: Final[frozenset[str]] = frozenset(
    {"feat", "fix", "refactor", "docs", "test", "chore", "other"}
)

# How many commits to ask :func:`build_changelog` for. 200 keeps the
# page snappy while still covering the last several weeks on a busy
# repo. The same cap is used for the JSON endpoint so the two views
# stay coherent.
_DEFAULT_LIMIT: Final[int] = 200


@router.get("/changelog", response_class=HTMLResponse)
async def changelog_page(request: Request, kind: str | None = None) -> HTMLResponse:
    """Render the changelog HTML page.

    The ``kind`` query-string is normalised against :data:`_KNOWN_KINDS`
    — anything outside the whitelist is treated as "no filter" so a
    bookmarked URL with a stale kind doesn't 404 on the user.
    """
    selected_kind = _normalise_kind(kind)

    try:
        entries = await build_changelog(limit=_DEFAULT_LIMIT)
    except GitUnavailableError as exc:
        log.warning("changelog.page.unavailable", error=str(exc))
        return templates.TemplateResponse(
            request,
            "changelog.html",
            {
                "title": "Changelog",
                "active_nav": "settings",
                "entries": [],
                "selected_kind": None,
                "kinds": sorted(_KNOWN_KINDS),
                "kind_counts": {},
                "unavailable": True,
                "unavailable_reason": str(exc),
            },
            status_code=200,
        )

    visible = _filter_entries(entries, selected_kind)
    kind_counts = _count_by_kind(entries)
    log.info(
        "changelog.page.served",
        total=len(entries),
        visible=len(visible),
        kind=selected_kind,
    )
    return templates.TemplateResponse(
        request,
        "changelog.html",
        {
            "title": "Changelog",
            "active_nav": "settings",
            "entries": visible,
            "selected_kind": selected_kind,
            "kinds": sorted(_KNOWN_KINDS),
            "kind_counts": kind_counts,
            "unavailable": False,
            "unavailable_reason": None,
        },
    )


@router.get("/api/changelog.json")
async def changelog_json(kind: str | None = None) -> JSONResponse:
    """JSON twin of the HTML page.

    Returns ``404`` when git is unavailable — the body is a structured
    ``{"detail": "..."}`` rather than the bare FastAPI default so a
    client can distinguish "no commits" from "no git" without parsing
    free-form prose.
    """
    selected_kind = _normalise_kind(kind)

    try:
        entries = await build_changelog(limit=_DEFAULT_LIMIT)
    except GitUnavailableError as exc:
        log.warning("changelog.json.unavailable", error=str(exc))
        raise HTTPException(
            status_code=404,
            detail=f"Changelog unavailable: {exc}",
        ) from exc

    visible = _filter_entries(entries, selected_kind)
    log.info(
        "changelog.json.served",
        total=len(entries),
        visible=len(visible),
        kind=selected_kind,
    )
    return JSONResponse(
        {
            "entries": list(visible),
            "kind": selected_kind,
            "total": len(entries),
            "visible": len(visible),
        }
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_kind(raw: str | None) -> str | None:
    """Return the ``kind`` query-string if it's a known bucket, else ``None``.

    Whitespace and case are normalised so ``?kind=FEAT`` and
    ``?kind=feat `` both resolve cleanly. Unknown kinds collapse to
    ``None`` (i.e. "no filter") so a stale link can't 404 the page.
    """
    if raw is None:
        return None
    token = raw.strip().lower()
    if not token:
        return None
    if token in _KNOWN_KINDS:
        return token
    return None


def _filter_entries(
    entries: list[ChangelogEntry],
    kind: str | None,
) -> list[ChangelogEntry]:
    """Apply the ``kind`` filter — returns the original list when ``kind`` is None."""
    if kind is None:
        return entries
    return [e for e in entries if e["kind"] == kind]


def _count_by_kind(entries: list[ChangelogEntry]) -> dict[str, int]:
    """Tally entries per ``kind`` so the filter chips can show counts."""
    counts: dict[str, int] = dict.fromkeys(_KNOWN_KINDS, 0)
    for entry in entries:
        bucket = entry["kind"] if entry["kind"] in counts else "other"
        counts[bucket] += 1
    return counts


__all__ = ["router"]
