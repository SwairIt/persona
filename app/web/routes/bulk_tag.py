"""Admin web UI for the structured bulk tag/untag operation.

Wraps :func:`app.bulk_tag.preview_bulk_tag` and
:func:`app.bulk_tag.apply_bulk_tag` behind three endpoints:

* ``GET  /admin/bulk-tag`` — render the form page.
* ``POST /api/bulk-tag/preview`` — JSON ``{count, sample}`` for the matching
  shots; never mutates.
* ``POST /api/bulk-tag/apply`` — JSON ``{affected, action, tag}`` after
  actually adding or removing the tag.

Unlike the FTS5-driven sibling routes (:mod:`app.web.routes.bulk_untag`,
:mod:`app.web.routes.bulk_pin`), this page lets the operator filter by
*structured* fields (app name, window-title substring, OCR substring, or
just a date range), which is faster to reason about during routine
re-tagging passes.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.bulk_tag import apply_bulk_tag, preview_bulk_tag
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.web.bulk_tag")

router = APIRouter(tags=["bulk-tag"])

_ALLOWED_FILTER_KINDS: frozenset[str] = frozenset(
    {"app", "window_contains", "ocr_contains", "date_only"}
)
_ALLOWED_ACTIONS: frozenset[str] = frozenset({"add", "remove"})

_TAG_MIN, _TAG_MAX = 1, 60
_FILTER_VALUE_MAX = 500
_DATE_MAX = 32


def _validate_filter_kind(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in _ALLOWED_FILTER_KINDS:
        msg = "filter_kind must be one of app|window_contains|ocr_contains|date_only"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_filter_value(value: str) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) > _FILTER_VALUE_MAX:
        msg = f"filter_value must be <= {_FILTER_VALUE_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_date(value: str | None) -> str | None:
    """Accept ``YYYY-MM-DD`` or empty/None — bounce anything else."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > _DATE_MAX:
        msg = "date string too long"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_tag(value: str) -> str:
    cleaned = (value or "").strip()
    if not (_TAG_MIN <= len(cleaned) <= _TAG_MAX):
        msg = f"tag must be {_TAG_MIN}..{_TAG_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned.lower()


def _validate_action(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in _ALLOWED_ACTIONS:
        msg = "action must be add or remove"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


@router.get("/admin/bulk-tag", response_class=HTMLResponse)
async def bulk_tag_page(request: Request) -> HTMLResponse:
    """Render the bulk-tag admin page."""
    return templates.TemplateResponse(
        request,
        "bulk_tag.html",
        {
            "title": "Bulk tag",
            "active_nav": "settings",
        },
    )


@router.post("/api/bulk-tag/preview")
async def bulk_tag_preview_api(
    filter_kind: str = Form(...),
    filter_value: str = Form(""),
    date_from: str | None = Form(None),
    date_to: str | None = Form(None),
) -> JSONResponse:
    """Return ``{count, sample}`` for the matching shots — no mutation."""
    kind = _validate_filter_kind(filter_kind)
    value = _validate_filter_value(filter_value)
    df = _validate_date(date_from)
    dt = _validate_date(date_to)

    try:
        result = await preview_bulk_tag(kind, value, df, dt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info(
        "preview",
        filter_kind=kind,
        filter_value=value,
        date_from=df,
        date_to=dt,
        count=result["count"],
    )
    return JSONResponse(result)


@router.post("/api/bulk-tag/apply")
async def bulk_tag_apply_api(
    filter_kind: str = Form(...),
    filter_value: str = Form(""),
    date_from: str | None = Form(None),
    date_to: str | None = Form(None),
    tag: str = Form(...),
    action: str = Form(...),
) -> JSONResponse:
    """Add or remove ``tag`` across every shot matching the filter."""
    kind = _validate_filter_kind(filter_kind)
    value = _validate_filter_value(filter_value)
    df = _validate_date(date_from)
    dt = _validate_date(date_to)
    tag_v = _validate_tag(tag)
    action_v = _validate_action(action)

    try:
        result = await apply_bulk_tag(kind, value, df, dt, tag_v, action_v)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info(
        "apply",
        filter_kind=kind,
        filter_value=value,
        date_from=df,
        date_to=dt,
        tag=tag_v,
        action=action_v,
        affected=result["affected"],
    )
    return JSONResponse(result)


__all__ = ["router"]
