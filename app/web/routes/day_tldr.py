"""Per-day TL;DR endpoints — lazy LLM-backed one-sentence day summary.

These endpoints are designed to be called from the page after first paint
(via fetch/HTMX) so the synchronous render path is never blocked on the LLM.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.llm.day_tldr import Status, summarise_day_tldr
from app.logging_setup import get_logger

router = APIRouter(tags=["day-tldr"])

log = get_logger("persona.day_tldr")


class _DayTldrPayload(TypedDict):
    day: str
    tldr: str
    status: Status
    cached: bool


def _validate_day(day: str) -> str:
    """Reject anything that isn't YYYY-MM-DD. Returns the canonical form."""
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="day must be in YYYY-MM-DD form",
        ) from exc
    return parsed.isoformat()


def _to_payload(day_iso: str, result: dict[str, object]) -> _DayTldrPayload:
    return {
        "day": day_iso,
        "tldr": str(result["tldr"]),
        "status": result["status"],  # type: ignore[typeddict-item]
        "cached": bool(result["cached"]),
    }


@router.get("/api/day-tldr.json", response_class=JSONResponse)
async def get_day_tldr(
    day: str = Query(..., description="Day in YYYY-MM-DD form"),
) -> JSONResponse:
    """Return cached TL;DR if present, else generate lazily.

    Response status is always 200; the JSON payload's ``status`` field carries
    the outcome (``ok`` / ``empty`` / ``missing_config``). This keeps the
    client-side fetch path simple — no try/catch around HTTP errors needed.
    """
    canonical = _validate_day(day)
    result = await summarise_day_tldr(canonical)
    return JSONResponse(_to_payload(canonical, dict(result)))


@router.post("/api/day-tldr/{day}/regenerate", response_class=JSONResponse)
async def regenerate_day_tldr(day: str) -> JSONResponse:
    """Force a fresh LLM call, overwriting any cached row for ``day``."""
    canonical = _validate_day(day)
    result = await summarise_day_tldr(canonical, force=True)
    if result["status"] == "missing_config":
        raise HTTPException(
            status_code=400,
            detail=(
                "LLM not configured. Set PERSONA_BYO_API_PROVIDER and "
                "PERSONA_BYO_API_KEY in .env to enable AI features."
            ),
        )
    return JSONResponse(_to_payload(canonical, dict(result)))
