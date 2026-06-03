"""Admin UI for the bulk notes CSV import (v1.8 feature 3/3).

Two endpoints under ``/admin/notes-csv-import``:

* ``GET  /admin/notes-csv-import`` — renders the upload form (file
  input + free-text tag list) plus the result summary from the last
  attempt when one was just made.
* ``POST /admin/notes-csv-import`` — accepts a ``multipart/form-data``
  upload (single ``file`` field, optional ``tags`` field), enforces the
  5 MiB cap, parses the body via the stdlib ``csv`` reader, and
  delegates to :func:`app.notes_csv_import.import_notes_csv`. Every
  call — accepted or rejected — is recorded in ``audit_log`` so an
  operator can audit who imported what after the fact.

The route deliberately renders the result inline (the template branches
on ``result is not None``) rather than 303-redirecting like
:mod:`app.web.routes.settings_backup` because the report is *the*
useful artefact here: a 303 to ``GET`` would drop the per-row error
list and force the operator to re-upload to see it.

Idempotency lives in :mod:`app.notes_csv_import` (sha-256 of the
normalised body); this route is just a thin HTTP shell.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.notes_csv_import import ImportResult, import_notes_csv
from app.web.templates_engine import templates

router = APIRouter(tags=["notes_csv_import"])

log = get_logger("persona.notes_csv_import.routes")

# Hard cap on the uploaded blob — matches the spec ("max 5MB"). The
# limit is checked *after* :meth:`UploadFile.read` because Starlette
# does not stream-truncate uploads; the trade-off is acceptable because
# the cap is small enough that buffering it whole is cheap and the next
# defensive line, FastAPI's own request-body size guard, kicks in
# upstream for genuinely hostile bodies.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _actor(request: Request) -> str | None:
    """Best-effort client identity for the audit log.

    Mirrors :func:`app.web.routes.settings_backup._actor` — Persona has
    no per-user session model so the client IP is the closest stable
    identity we have. ``request.client`` can be ``None`` when a test
    client builds the request without a transport, so we fall back to
    ``None`` rather than raising.
    """
    return request.client.host if request.client is not None else None


def _parse_tag_input(raw: str | None) -> list[str]:
    """Split the form-level ``tags`` field on commas, normalise, dedupe.

    Same rules as the per-row ``tags`` column inside
    :mod:`app.notes_csv_import`: split on comma, strip, lower-case,
    drop empties, deduplicate while preserving first-seen order. The
    function returns an empty list when the input is missing or blank
    so the importer can treat "no default tags" uniformly.
    """
    if not raw:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for piece in raw.split(","):
        cleaned = piece.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


def _render(
    request: Request,
    *,
    result: ImportResult | None,
    default_tags: list[str],
) -> HTMLResponse:
    """Shared template-render path for the GET and the POST."""
    return templates.TemplateResponse(
        request,
        "notes_csv_import.html",
        {
            "title": "Notes CSV import",
            "active_nav": "settings",
            "result": result,
            "default_tags": default_tags,
            "max_upload_bytes": _MAX_UPLOAD_BYTES,
        },
    )


@router.get("/admin/notes-csv-import", response_class=HTMLResponse)
async def notes_csv_import_page(request: Request) -> HTMLResponse:
    """Render the upload form with no result block (initial visit)."""
    return _render(request, result=None, default_tags=[])


@router.post("/admin/notes-csv-import", response_class=HTMLResponse)
async def notes_csv_import_run(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    tags: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Parse a multipart CSV upload and import its rows into ``notes``.

    The flow is intentionally synchronous from the operator's point of
    view: the request blocks until every row has either been inserted,
    skipped (hash-dedup) or recorded in the per-row error list. The
    same template is rendered twice — first with ``result=None``
    (initial visit), then with ``result=<dict>`` after the POST — so
    the operator never loses sight of the report.
    """
    actor = _actor(request)

    raw = await file.read()
    if len(raw) == 0:
        await log_action(
            action="notes_csv_import.upload",
            actor=actor,
            detail="empty upload",
            success=False,
        )
        raise HTTPException(status_code=400, detail="empty upload")
    if len(raw) > _MAX_UPLOAD_BYTES:
        await log_action(
            action="notes_csv_import.upload",
            actor=actor,
            detail=f"too large: {len(raw)}B",
            success=False,
        )
        raise HTTPException(status_code=413, detail="upload too large")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        await log_action(
            action="notes_csv_import.upload",
            actor=actor,
            detail=f"invalid utf-8: {exc}",
            success=False,
        )
        raise HTTPException(
            status_code=400, detail=f"invalid UTF-8: {exc}"
        ) from exc

    default_tags = _parse_tag_input(tags)
    result = await import_notes_csv(text, default_tags=default_tags)

    await log_action(
        action="notes_csv_import.upload",
        actor=actor,
        detail=(
            f"imported={result['imported']} "
            f"skipped={result['skipped']} "
            f"errors={len(result['errors'])} "
            f"bytes={len(raw)}"
        ),
        success=True,
    )
    log.info(
        "notes_csv_import.route.ok",
        actor=actor,
        bytes=len(raw),
        imported=result["imported"],
        skipped=result["skipped"],
        errors=len(result["errors"]),
        default_tags=default_tags,
    )
    return _render(request, result=result, default_tags=default_tags)


__all__ = ["router"]
