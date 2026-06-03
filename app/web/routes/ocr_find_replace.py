"""Admin UI for bulk regex find-and-replace across ``screenshots.ocr_text``.

Persona v0.77 feature 3/3. Three endpoints, all under ``/admin``:

* ``GET  /admin/ocr-find-replace``          — render the form page.
* ``POST /admin/ocr-find-replace/preview``  — HTMX fragment with a
  dry-run diff table (regex compiled, no rows touched).
* ``POST /admin/ocr-find-replace/apply``    — execute the substitution.
  Audit-logged with the row count so a later
  ``/audit?action=ocr.find_replace`` query can reconstruct exactly which
  pattern hit the DB and how much it changed.

Design notes
------------
* The regex is compiled inside :mod:`app.ocr_find_replace` and the
  :class:`ValueError` that wraps :class:`re.error` is mapped to a 400
  here so the user sees a real diagnostic instead of a 500 traceback.
* Both preview and apply share the same form field names
  (``pattern`` / ``replacement`` / ``limit``) so the apply form in the
  preview fragment can post straight back without server-side
  bookkeeping.
* Apply is audit-logged with the resolved actor (client host) so the
  ``/audit`` reader shows who triggered the bulk write.
* Both endpoints accept an optional ``app_name`` form field (v1.1).
  Empty / whitespace-only values mean "no scope" and reproduce the
  v0.77 corpus-wide behaviour; non-empty values are forwarded to the
  service layer which adds a parametrised ``WHERE app_name = ?``
  predicate to the candidate query. The value is also threaded through
  the preview→apply HTMX flow as a hidden field so the user cannot
  accidentally widen the scope between dry-run and write.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.ocr_find_replace import apply as ocr_apply
from app.ocr_find_replace import preview as ocr_preview
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-find-replace"])
log = get_logger("persona.ocr.find_replace")
log_per_app = get_logger("persona.ocr.find_replace.per_app")

_APP_NAME_MAX_LEN = 200

_PREVIEW_LIMIT_DEFAULT = 100
_PREVIEW_LIMIT_MAX = 500
_APPLY_LIMIT_DEFAULT = 1_000
_APPLY_LIMIT_MAX = 10_000


def _validate_pattern(pattern: str) -> str:
    """Reject empty / whitespace-only patterns up front."""
    cleaned = (pattern or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Pattern must not be empty.")
    # Length cap mirrors the FTS bulk-delete admin so the form cannot
    # ship megabyte-sized "patterns" through the request body.
    if len(cleaned) > 1_000:
        raise HTTPException(
            status_code=400, detail="Pattern is suspiciously long (>1000 chars)."
        )
    return cleaned


def _validate_limit(limit: int, *, cap: int) -> int:
    """Clamp the user-supplied limit into ``1..cap``."""
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="limit must be an integer.") from exc
    if value < 1 or value > cap:
        raise HTTPException(status_code=400, detail=f"limit must be 1..{cap}.")
    return value


def _validate_app_name(app_name: str | None) -> str | None:
    """Normalise the optional app-name scope.

    Empty / whitespace-only input is collapsed to ``None`` so the service
    layer sees a single "no scope" sentinel rather than two equivalent
    forms. Non-empty values are stripped and length-capped — long values
    would still be safe (they go through a bound parameter) but a
    multi-kilobyte ``app_name`` form field is always a typo or abuse.
    """
    if app_name is None:
        return None
    cleaned = app_name.strip()
    if not cleaned:
        return None
    if len(cleaned) > _APP_NAME_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"app_name is too long (>{_APP_NAME_MAX_LEN} chars).",
        )
    return cleaned


@router.get("/admin/ocr-find-replace", response_class=HTMLResponse)
async def ocr_find_replace_page(request: Request) -> HTMLResponse:
    """Render the form page (empty preview slot, no rows touched)."""
    return templates.TemplateResponse(
        request,
        "ocr_find_replace.html",
        {
            "title": "OCR find & replace",
            "active_nav": "settings",
            "preview_default": _PREVIEW_LIMIT_DEFAULT,
            "apply_default": _APPLY_LIMIT_DEFAULT,
            "preview_max": _PREVIEW_LIMIT_MAX,
            "apply_max": _APPLY_LIMIT_MAX,
        },
    )


@router.post("/admin/ocr-find-replace/preview", response_class=HTMLResponse)
async def ocr_find_replace_preview(
    request: Request,
    pattern: str = Form(...),
    replacement: str = Form(default=""),
    limit: int = Form(_PREVIEW_LIMIT_DEFAULT),
    app_name: str = Form(default=""),
) -> HTMLResponse:
    """Run the regex as a dry-run and return the HTMX preview fragment."""
    pattern_v = _validate_pattern(pattern)
    limit_v = _validate_limit(limit, cap=_PREVIEW_LIMIT_MAX)
    app_name_v = _validate_app_name(app_name)
    try:
        rows = await ocr_preview(
            pattern_v, replacement, limit=limit_v, app_name=app_name_v
        )
    except ValueError as exc:
        # Bad regex — render the same fragment but with an error
        # message instead of a results table. Status stays 200 so HTMX
        # swaps the fragment in place (a 4xx would replace #target with
        # nothing and lose the user's typed pattern).
        return templates.TemplateResponse(
            request,
            "_ocr_find_replace_fragment.html",
            {
                "preview_error": str(exc),
                "preview_pattern": pattern_v,
                "preview_replacement": replacement,
                "preview_limit": limit_v,
                "preview_app_name": app_name_v or "",
                "apply_default": _APPLY_LIMIT_DEFAULT,
                "apply_max": _APPLY_LIMIT_MAX,
            },
        )

    if app_name_v is not None:
        log_per_app.info(
            "ocr.find_replace.route.preview.per_app",
            pattern=pattern_v,
            replacement=replacement,
            limit=limit_v,
            matched=len(rows),
            app_name=app_name_v,
        )

    return templates.TemplateResponse(
        request,
        "_ocr_find_replace_fragment.html",
        {
            "preview_rows": rows,
            "preview_pattern": pattern_v,
            "preview_replacement": replacement,
            "preview_limit": limit_v,
            "preview_app_name": app_name_v or "",
            "apply_default": _APPLY_LIMIT_DEFAULT,
            "apply_max": _APPLY_LIMIT_MAX,
        },
    )


@router.post("/admin/ocr-find-replace/apply", response_class=HTMLResponse)
async def ocr_find_replace_apply(
    request: Request,
    pattern: str = Form(...),
    replacement: str = Form(default=""),
    limit: int = Form(_APPLY_LIMIT_DEFAULT),
    app_name: str = Form(default=""),
) -> HTMLResponse:
    """Execute the regex substitution and return the result fragment."""
    pattern_v = _validate_pattern(pattern)
    limit_v = _validate_limit(limit, cap=_APPLY_LIMIT_MAX)
    app_name_v = _validate_app_name(app_name)
    actor = request.client.host if request.client is not None else None

    try:
        result = await ocr_apply(
            pattern_v, replacement, limit=limit_v, app_name=app_name_v
        )
    except ValueError as exc:
        log.warning(
            "ocr.find_replace.apply.bad_regex",
            pattern=pattern_v,
            actor=actor,
            error=str(exc),
            app_name=app_name_v,
        )
        await log_action(
            action="ocr.find_replace",
            actor=actor,
            target="screenshots.ocr_text",
            detail=f"bad regex: {exc}",
            success=False,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Build the audit detail string. When ``app_name`` is set we include
    # it so a later ``/audit?action=ocr.find_replace`` query can tell
    # scoped from corpus-wide runs at a glance; when it's not, we omit
    # the field entirely to keep the v0.77 detail format unchanged for
    # existing log readers.
    scope_suffix = f" app_name={app_name_v!r}" if app_name_v is not None else ""
    await log_action(
        action="ocr.find_replace",
        actor=actor,
        target="screenshots.ocr_text",
        detail=(
            f"pattern={pattern_v!r} replacement={replacement!r} "
            f"limit={limit_v} scanned={result['scanned']} "
            f"changed={result['changed']}{scope_suffix}"
        ),
        success=True,
    )
    if app_name_v is not None:
        log_per_app.info(
            "ocr.find_replace.route.apply.per_app",
            pattern=pattern_v,
            replacement=replacement,
            limit=limit_v,
            scanned=result["scanned"],
            changed=result["changed"],
            app_name=app_name_v,
            actor=actor,
        )

    return templates.TemplateResponse(
        request,
        "_ocr_find_replace_fragment.html",
        {
            "apply_result": result,
            "apply_app_name": app_name_v or "",
            "apply_default": _APPLY_LIMIT_DEFAULT,
            "apply_max": _APPLY_LIMIT_MAX,
        },
    )


__all__ = ["router"]
