"""Audit log replay — read-only narrative view (v0.90 feature 1/3).

* ``GET /audit/replay/{day}``           renders ``audit_replay.html``
  with one collapsible block per action-category section.
* ``GET /api/audit/replay/{day}.json``  returns the same payload as JSON
  for tooling / dashboards.

The actual grouping lives in :func:`app.audit_replay.build_replay`; this
module is a thin HTTP shell. All SQL is parametrised inside
``audit_replay`` — no user input ever touches a SQL string here.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` (task spec forbids touching ``main.py``). Wire it
up in a follow-up patch with::

    from app.web.routes import audit_replay as audit_replay_routes
    app.include_router(audit_replay_routes.router)
"""

from __future__ import annotations

from typing import TypedDict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.audit_replay import ReplayEntry, build_replay
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["audit-replay"])
log = get_logger("persona.audit.replay")


class _RenderedSection(TypedDict):
    """One category bucket as projected onto the Jinja template context.

    Kept private — only the route layer needs the rendered shape; the
    data module's :class:`app.audit_replay.ReplaySection` stays
    label-free so JSON consumers aren't pinned to English.
    """

    category: str
    label: str
    entries: list[ReplayEntry]
    count: int

# Human-friendly labels for the five canonical category buckets. Lives
# in the route module (not :mod:`app.audit_replay`) because it is a UI
# concern — the data-side module deliberately stays language-agnostic so
# tooling that consumes the JSON endpoint isn't pinned to English.
_CATEGORY_LABELS: dict[str, str] = {
    "settings": "Settings changes",
    "bulk_delete": "Bulk deletes",
    "tokens": "Tokens",
    "vault": "Vault & encrypted notes",
    "other": "Other actions",
}


def _label_for(category: str) -> str:
    """Return the human label for ``category`` (fallback: the raw slug)."""
    return _CATEGORY_LABELS.get(category, category)


@router.get("/audit/replay/{day}", response_class=HTMLResponse)
async def audit_replay_page(request: Request, day: str) -> HTMLResponse:
    """Render the per-day audit replay as collapsible category sections.

    Every one of the five canonical sections is rendered every time so
    the page layout stays predictable — empty buckets show a friendly
    "no entries" placeholder rather than collapsing the layout. Sections
    use ``<details>``/``<summary>`` so collapse state survives without
    JS, with the first non-empty section pre-expanded for the common
    "what just happened today" review case.
    """
    payload = await build_replay(day)
    sections: list[_RenderedSection] = [
        _RenderedSection(
            category=section["category"],
            label=_label_for(section["category"]),
            entries=list(section["entries"]),
            count=len(section["entries"]),
        )
        for section in payload["sections"]
    ]
    # Pre-expand the first section that actually has entries. Falls back
    # to the first section (``settings``) if the whole day is empty so
    # we always have *something* expanded for the page to look settled.
    first_open = next(
        (idx for idx, section in enumerate(sections) if section["count"] > 0),
        0,
    )
    log.info(
        "audit.replay.page",
        day=payload["day"],
        total=payload["total"],
        truncated=payload["truncated"],
    )
    return templates.TemplateResponse(
        request,
        "audit_replay.html",
        {
            "title": f"Audit replay — {payload['day']}",
            "active_nav": "settings",
            "day": payload["day"],
            "sections": sections,
            "total": payload["total"],
            "truncated": payload["truncated"],
            "first_open": first_open,
        },
    )


@router.get("/api/audit/replay/{day}.json", response_class=JSONResponse)
async def audit_replay_json(day: str) -> JSONResponse:
    """Machine-readable companion to :func:`audit_replay_page`.

    Same payload shape as :func:`app.audit_replay.build_replay` — the
    route just adds the ``label`` field on each section so JSON
    consumers don't have to duplicate the English-label map.
    """
    payload = await build_replay(day)
    sections = [
        {
            "category": section["category"],
            "label": _label_for(section["category"]),
            "entries": list(section["entries"]),
        }
        for section in payload["sections"]
    ]
    log.info(
        "audit.replay.json",
        day=payload["day"],
        total=payload["total"],
        truncated=payload["truncated"],
    )
    return JSONResponse(
        {
            "day": payload["day"],
            "total": payload["total"],
            "truncated": payload["truncated"],
            "sections": sections,
        }
    )


__all__ = ["router"]
