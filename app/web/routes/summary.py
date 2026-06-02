"""Daily-summary endpoint — triggers BYO LLM over last 24h captures."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.llm import LLMNotConfigured, summarise_day
from app.web.templates_engine import templates

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("/", response_class=HTMLResponse)
async def summary_page(
    request: Request,
    date: str | None = Query(default=None),
) -> HTMLResponse:
    target = _parse_date(date)
    return templates.TemplateResponse(
        request,
        "summary.html",
        {
            "title": "Daily summary",
            "active_nav": "summary",
            "target_day": target,
        },
    )


@router.post("/generate", response_class=JSONResponse)
async def summary_generate(date: str | None = Query(default=None)) -> JSONResponse:
    target = _parse_date(date)
    try:
        text = await summarise_day(target)
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"date": target.strftime("%Y-%m-%d"), "summary": text})


def _parse_date(value: str | None) -> datetime:
    if not value:
        now = datetime.now().astimezone()
        return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
