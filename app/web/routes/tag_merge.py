"""HTMX-driven admin UI for :func:`app.tag_merge.merge_tags`.

Three endpoints, all under ``/admin/tag-merge``:

* ``GET  /admin/tag-merge``       — render the page with two tag-name
  inputs (datalist-autocompleted from existing tags) and a preview
  button. No DB writes.
* ``POST /admin/tag-merge``       — body has ``source``, ``dest`` and
  an optional ``confirm`` flag.

  * **No ``confirm``** → run the merge in dry-run mode and return the
    preview fragment with the move count and a Confirm button.
  * **``confirm`` present** → actually execute the merge and return a
    success / error fragment.

This file deliberately stays a thin adapter: every interesting decision
lives in :mod:`app.tag_merge`. We just translate form fields, render
HTML, and emit one audit row on bad inputs so an attacker probing the
endpoint can be traced.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.tags import list_tags
from app.tag_merge import merge_tags
from app.web.templates_engine import templates

log = get_logger("persona.web.tag_merge")

router = APIRouter(tags=["tag-merge"])

# Tag names are short by convention; cap to a comfortable upper bound
# so a typo'd 1MB POST body can't push the page through the form-decoder.
_NAME_MAX = 200


def _clean_name(raw: str, *, field: str) -> str:
    """Trim + lowercase a submitted tag name, rejecting empties / over-longs."""
    cleaned = (raw or "").strip().lower()
    if not cleaned:
        msg = f"{field} must not be empty"
        raise HTTPException(status_code=400, detail=msg)
    if len(cleaned) > _NAME_MAX:
        msg = f"{field} must be at most {_NAME_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


async def _load_tag_names() -> list[str]:
    """Return every existing tag name so the datalist can suggest them."""
    async with get_connection() as conn:
        rows = await list_tags(conn)
    return [str(row["name"]) for row in rows]


@router.get("/admin/tag-merge", response_class=HTMLResponse)
async def tag_merge_page(request: Request) -> HTMLResponse:
    """Render the merge form with a populated datalist of existing tags."""
    names = await _load_tag_names()
    return templates.TemplateResponse(
        request,
        "tag_merge.html",
        {
            "title": "Merge tags",
            "active_nav": "settings",
            "tag_names": names,
        },
    )


@router.post("/admin/tag-merge", response_class=HTMLResponse)
async def tag_merge_submit(
    request: Request,
    source: str = Form(...),
    dest: str = Form(...),
    confirm: str | None = Form(default=None),
) -> HTMLResponse:
    """Run a dry-run preview, or — when ``confirm`` is supplied — execute the merge.

    Both branches return the same template fragment (``tag_merge.html``
    with ``result_*`` context keys) so HTMX can swap a single target on
    the page. The preview branch additionally surfaces a Confirm button
    that POSTs back here with ``confirm=1``.
    """
    source_v = _clean_name(source, field="source")
    dest_v = _clean_name(dest, field="dest")

    if source_v == dest_v:
        # Caller asked to merge a tag into itself — surface that as a
        # form error instead of letting the audit log fill up with
        # confusing no-op rows.
        await log_action(
            "tag.merge",
            target=f"{source_v}->{dest_v}",
            detail="source equals destination",
            success=False,
        )
        names = await _load_tag_names()
        return templates.TemplateResponse(
            request,
            "tag_merge.html",
            {
                "title": "Merge tags",
                "active_nav": "settings",
                "tag_names": names,
                "submitted_source": source_v,
                "submitted_dest": dest_v,
                "error": "Source and destination must differ.",
            },
        )

    is_confirm = confirm is not None and confirm.strip() != ""
    result = await merge_tags(source_v, dest_v, dry_run=not is_confirm)

    names = await _load_tag_names()
    context = {
        "title": "Merge tags",
        "active_nav": "settings",
        "tag_names": names,
        "submitted_source": source_v,
        "submitted_dest": dest_v,
        "result_moved": result["moved"],
        "result_source_existed": result["source_existed"],
        "result_dest_existed": result["dest_existed"],
        "result_dry_run": result["dry_run"],
        "result_committed": (not result["dry_run"])
        and result["source_existed"]
        and result["dest_existed"],
    }
    return templates.TemplateResponse(request, "tag_merge.html", context)


__all__ = ["router"]
