"""Capture-quality lab — HTML dashboard + JSON + sampler trigger.

Three URLs sit on top of :mod:`app.quality_sampler`:

* ``POST /api/quality-sample/run`` — fire-and-forget. Schedules
  :func:`app.quality_sampler.sample_recent` on FastAPI's
  ``BackgroundTasks`` so the HTTP request returns within a few
  milliseconds while the (potentially slow, NumPy-heavy) sampling
  proceeds out-of-band. Returns ``202 Accepted`` with a small JSON
  envelope describing what was scheduled.
* ``GET  /api/quality-sample/bands.json`` — the aggregated per-band
  averages, ready for ad-hoc scripting and the dashboard's optional
  client-side refresh.
* ``GET  /stats/quality-lab`` — Tailwind page that renders the bands
  table and the "Запустить замер" button.

The route layer is intentionally thin — it never touches the DB
directly, only formats values for the template. All SQL lives in
:mod:`app.quality_sampler`.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.quality_sampler import aggregate_by_band, sample_recent
from app.web.templates_engine import templates

log = get_logger("persona.quality_lab")

router = APIRouter(tags=["quality-lab"])

# Mirrors :data:`app.quality_sampler._DEFAULT_LIMIT`. Duplicated here so
# the pydantic body schema has a literal default even if someone reads
# the OpenAPI export without the sampler module in scope. The 1..500
# bounds match the sampler's internal clamp so an out-of-band JSON
# payload gets a clear 422 instead of being silently clipped.
_DEFAULT_LIMIT: Final[int] = 50
_MIN_LIMIT: Final[int] = 1
_MAX_LIMIT: Final[int] = 500


class _SampleRunBody(BaseModel):
    """JSON body for ``POST /api/quality-sample/run``."""

    limit: int = Field(default=_DEFAULT_LIMIT, ge=_MIN_LIMIT, le=_MAX_LIMIT)


async def _run_sampler_background(limit: int) -> None:
    """Background-task wrapper that swallows exceptions into the log.

    FastAPI's ``BackgroundTasks`` runs the coroutine after the response
    is sent; an unhandled exception inside would surface only in stderr
    and never reach the client. Wrap it so the logger picks it up under
    the canonical ``persona.quality_lab`` name with the same shape the
    rest of the project uses.
    """
    try:
        result = await sample_recent(limit=limit)
        log.info("quality_lab.background_done", **result)
    except Exception as exc:
        log.error(
            "quality_lab.background_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )


@router.post("/api/quality-sample/run", response_class=JSONResponse)
async def run_quality_sample(
    background_tasks: BackgroundTasks,
    body: _SampleRunBody | None = None,
) -> JSONResponse:
    """Schedule a sampler pass; return ``202`` immediately."""
    effective = body or _SampleRunBody()
    background_tasks.add_task(_run_sampler_background, effective.limit)
    log.info("quality_lab.scheduled", limit=effective.limit)
    return JSONResponse(
        {"status": "started", "limit": effective.limit},
        status_code=202,
    )


@router.get("/api/quality-sample/bands.json", response_class=JSONResponse)
async def quality_sample_bands_json() -> JSONResponse:
    """Return the per-band aggregation as JSON."""
    rows = await aggregate_by_band()
    return JSONResponse({"bands": rows})


def _format_optional_float(value: float | None, digits: int = 1) -> str:
    """Render a possibly-NULL average as a fixed-precision string or em-dash."""
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _format_optional_bytes(value: float | None) -> str:
    """Render an average file size in KB (one decimal) or em-dash."""
    if value is None:
        return "—"
    return f"{value / 1024.0:.1f} KB"


def _present_bands(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decorate raw aggregation rows with display strings for the template."""
    presented: list[dict[str, Any]] = []
    for row in rows:
        presented.append(
            {
                "quality_used": row["quality_used"],
                "width_used": row["width_used"],
                "sample_count": row["sample_count"],
                "avg_sharpness_display": _format_optional_float(
                    row["avg_sharpness"], digits=1
                ),
                "avg_ocr_chars_display": _format_optional_float(
                    row["avg_ocr_chars"], digits=0
                ),
                "avg_file_size_display": _format_optional_bytes(
                    row["avg_file_size_bytes"]
                ),
                "avg_phash_entropy_display": _format_optional_float(
                    row["avg_phash_entropy_bits"], digits=1
                ),
            }
        )
    return presented


@router.get("/stats/quality-lab", response_class=HTMLResponse)
async def quality_lab_page(request: Request) -> HTMLResponse:
    """Render the lab dashboard with the bands table and run button."""
    raw_bands = await aggregate_by_band()
    bands = _present_bands(raw_bands)
    return templates.TemplateResponse(
        request,
        "quality_lab.html",
        {
            "title": "Качество захвата",
            "active_nav": "stats",
            "bands": bands,
        },
    )
