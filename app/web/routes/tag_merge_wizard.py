"""Guided multi-step wizard around :func:`app.tag_merge.merge_tags` (v0.96).

The classic /admin/tag-merge page asks the operator to type both tag
names into free-form inputs and then run a preview. That works when
you already know which two tags you want to fold together, but the
typical use case is the opposite: operators *discover* a duplicate
while browsing the tag list and then want to be walked through the
merge without having to remember the destination's exact spelling.

This wizard is that walk-through. Four endpoints, all under
``/admin/tag-merge-wizard``:

* ``GET /admin/tag-merge-wizard`` — full page render. Step 1: pick a
  source tag from a radio list of every existing tag (showing
  assignment counts so the operator sees the blast radius before they
  even start).
* ``GET /admin/tag-merge-wizard/preview`` — HTMX fragment. Step 2:
  echoes the chosen source and renders the destination chooser
  (radio list of every *other* tag).
* ``GET /admin/tag-merge-wizard/confirm`` — HTMX fragment. Step 3:
  runs ``merge_tags(..., dry_run=True)`` and shows the count plus a
  Confirm button.
* ``POST /admin/tag-merge-wizard/apply`` — HTMX fragment. Step 4:
  runs the real merge via :func:`app.tag_merge.merge_tags` and renders
  a success / failure panel.

Each step is loaded into the same ``#tmw-wizard`` swap target via
``hx-get`` / ``hx-post`` so the page reads top-to-bottom and the
operator never sees a full reload between steps. The final apply step
delegates 100% of the mutation + audit work to the v0.48
``app.tag_merge`` module — this router is a thin orchestration layer
and writes only its own ``tag.merge.wizard.*`` audit rows around the
edges (one when a step is entered with bad input, one when apply is
called) so a security review can reconstruct exactly which operator
walked which path.

The whole feature is GET-heavy by design: steps 1-3 are idempotent
previews so refreshing or back-buttoning never has side effects. Only
step 4 (``/apply``) is a POST and that is the one and only place where
the database is mutated.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.tags import list_tags
from app.tag_merge import merge_tags
from app.web.templates_engine import templates

log = get_logger("persona.tag_merge_wizard")

router = APIRouter(tags=["tag-merge-wizard"])

# Tag names are short by convention; cap at the same upper bound the
# v0.48 /admin/tag-merge page uses so a typo'd 1MB query string can't
# slip through the form decoder and pin the template render.
_NAME_MAX = 200


def _clean_name(raw: str | None, *, field: str) -> str:
    """Trim + lowercase a submitted tag name, rejecting empties / over-longs.

    Mirrors the validation in :mod:`app.web.routes.tag_merge` so the two
    pages reject identical inputs identically — operators bouncing
    between the classic and wizard UIs should never see one accept what
    the other refuses.
    """
    cleaned = (raw or "").strip().lower()
    if not cleaned:
        msg = f"{field} must not be empty"
        raise HTTPException(status_code=400, detail=msg)
    if len(cleaned) > _NAME_MAX:
        msg = f"{field} must be at most {_NAME_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


async def _load_tags() -> list[dict[str, Any]]:
    """Return every tag with its assignment count, ordered by count desc.

    :func:`app.storage.tags.list_tags` already returns rows shaped as
    ``{id, name, color, count}`` and sorts by ``COUNT(*) DESC, name``,
    which is exactly the ordering an operator picking a duplicate
    wants — the noisiest tags surface first.
    """
    async with get_connection() as conn:
        return await list_tags(conn)


# ---------------------------------------------------------------------------
# Step 1 — pick source
# ---------------------------------------------------------------------------


@router.get("/admin/tag-merge-wizard", response_class=HTMLResponse)
async def wizard_page(request: Request) -> HTMLResponse:
    """Render the wizard shell with Step 1 (pick source) already filled in.

    The full page extends ``base.html`` and seeds ``#tmw-wizard`` with
    the source chooser. Steps 2-4 are loaded into that same container
    via HTMX, so the operator never sees a full page reload.
    """
    tags = await _load_tags()
    log.info("tag_merge_wizard.page", tags=len(tags))
    return templates.TemplateResponse(
        request,
        "tag_merge_wizard.html",
        {
            "title": "Merge tags — wizard",
            "active_nav": "settings",
            "tags": tags,
            "step": 1,
        },
    )


# ---------------------------------------------------------------------------
# Step 2 — pick destination
# ---------------------------------------------------------------------------


@router.get("/admin/tag-merge-wizard/preview", response_class=HTMLResponse)
async def wizard_pick_dest(request: Request, source: str) -> HTMLResponse:
    """HTMX fragment: confirm the source and render the destination chooser.

    The source name is validated with :func:`_clean_name`; an empty or
    over-long value would have failed validation back in Step 1 so a
    400 here means somebody is poking the URL directly — log it once
    so the audit trail has a breadcrumb but otherwise let FastAPI's
    default 400 fall through.
    """
    source_v = _clean_name(source, field="source")
    tags = await _load_tags()

    # Confirm the picked source still exists. If it vanished between
    # steps (another tab deleted the tag, or the operator hand-edited
    # the URL) we surface that as a Step 1 reset with an error message
    # so the wizard can't dead-end on a phantom source.
    source_row = next((t for t in tags if t["name"] == source_v), None)
    if source_row is None:
        await log_action(
            "tag.merge.wizard.step2",
            target=source_v,
            detail="source tag does not exist",
            success=False,
        )
        log.info("tag_merge_wizard.step2_missing_source", source=source_v)
        return templates.TemplateResponse(
            request,
            "tag_merge_wizard.html",
            {
                "title": "Merge tags — wizard",
                "active_nav": "settings",
                "tags": tags,
                "step": 1,
                "fragment_only": True,
                "error": (
                    f"Tag {source_v!r} no longer exists. Start over below."
                ),
            },
        )

    # Destination candidates = every tag *except* the chosen source.
    # We strip the source rather than letting the template do it so
    # the count rendered in the picker ("N other tags") is honest.
    dest_candidates = [t for t in tags if t["name"] != source_v]

    log.info(
        "tag_merge_wizard.step2",
        source=source_v,
        source_count=int(source_row["count"]),        candidates=len(dest_candidates),
    )
    return templates.TemplateResponse(
        request,
        "tag_merge_wizard.html",
        {
            "title": "Merge tags — wizard",
            "active_nav": "settings",
            "step": 2,
            "fragment_only": True,
            "source_name": source_v,
            "source_count": int(source_row["count"]),            "dest_candidates": dest_candidates,
        },
    )


# ---------------------------------------------------------------------------
# Step 3 — dry-run preview + confirm button
# ---------------------------------------------------------------------------


@router.get("/admin/tag-merge-wizard/confirm", response_class=HTMLResponse)
async def wizard_preview(request: Request, source: str, dest: str) -> HTMLResponse:
    """HTMX fragment: run a dry-run merge and show the Confirm button.

    Reuses :func:`app.tag_merge.merge_tags` with ``dry_run=True`` so
    the count we display is exactly the count the apply step will
    move — no separate "preview SQL" path that could drift from the
    real mutation.
    """
    source_v = _clean_name(source, field="source")
    dest_v = _clean_name(dest, field="dest")

    if source_v == dest_v:
        # Caller routed back to /confirm with matching names. Log it
        # and re-render Step 2 with an error so the wizard doesn't
        # silently dead-end.
        await log_action(
            "tag.merge.wizard.step3",
            target=f"{source_v}->{dest_v}",
            detail="source equals destination",
            success=False,
        )
        tags = await _load_tags()
        dest_candidates = [t for t in tags if t["name"] != source_v]
        source_row = next((t for t in tags if t["name"] == source_v), None)
        return templates.TemplateResponse(
            request,
            "tag_merge_wizard.html",
            {
                "title": "Merge tags — wizard",
                "active_nav": "settings",
                "step": 2,
                "fragment_only": True,
                "source_name": source_v,
                "source_count": int(source_row["count"]) if source_row else 0,
                "dest_candidates": dest_candidates,
                "error": "Source and destination must differ — pick a different destination.",
            },
        )

    result = await merge_tags(source_v, dest_v, dry_run=True)

    log.info(
        "tag_merge_wizard.step3",
        source=source_v,
        dest=dest_v,
        moved=result["moved"],
        source_existed=result["source_existed"],
        dest_existed=result["dest_existed"],
    )
    return templates.TemplateResponse(
        request,
        "tag_merge_wizard.html",
        {
            "title": "Merge tags — wizard",
            "active_nav": "settings",
            "step": 3,
            "fragment_only": True,
            "source_name": source_v,
            "dest_name": dest_v,
            "result_moved": result["moved"],
            "result_source_existed": result["source_existed"],
            "result_dest_existed": result["dest_existed"],
        },
    )


# ---------------------------------------------------------------------------
# Step 4 — apply the merge
# ---------------------------------------------------------------------------


@router.post("/admin/tag-merge-wizard/apply", response_class=HTMLResponse)
async def wizard_apply(
    request: Request,
    source: str = Form(...),
    dest: str = Form(...),
) -> HTMLResponse:
    """HTMX fragment: execute the merge and render the result panel.

    The heavy lifting (transaction, INSERT OR IGNORE dedup, source
    cleanup, audit row) lives in :func:`app.tag_merge.merge_tags`. We
    add one extra audit row (``tag.merge.wizard.apply``) here so the
    audit log shows the wizard was the entry point, separately from
    the inner ``tag.merge`` row the merge function emits on success.
    """
    source_v = _clean_name(source, field="source")
    dest_v = _clean_name(dest, field="dest")

    if source_v == dest_v:
        await log_action(
            "tag.merge.wizard.apply",
            target=f"{source_v}->{dest_v}",
            detail="source equals destination",
            success=False,
        )
        return templates.TemplateResponse(
            request,
            "tag_merge_wizard.html",
            {
                "title": "Merge tags — wizard",
                "active_nav": "settings",
                "step": 4,
                "fragment_only": True,
                "source_name": source_v,
                "dest_name": dest_v,
                "apply_error": "Source and destination must differ.",
            },
        )

    await log_action(
        "tag.merge.wizard.apply",
        target=f"{source_v}->{dest_v}",
        detail="wizard apply requested",
    )
    result = await merge_tags(source_v, dest_v, dry_run=False)

    committed = (
        not result["dry_run"]
        and result["source_existed"]
        and result["dest_existed"]
    )

    log.info(
        "tag_merge_wizard.step4",
        source=source_v,
        dest=dest_v,
        moved=result["moved"],
        committed=committed,
    )
    return templates.TemplateResponse(
        request,
        "tag_merge_wizard.html",
        {
            "title": "Merge tags — wizard",
            "active_nav": "settings",
            "step": 4,
            "fragment_only": True,
            "source_name": source_v,
            "dest_name": dest_v,
            "result_moved": result["moved"],
            "result_source_existed": result["source_existed"],
            "result_dest_existed": result["dest_existed"],
            "committed": committed,
        },
    )


__all__ = ["router"]
