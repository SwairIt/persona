"""Diagnostic page — pretty wrapper around :func:`app.diagnostics.run_doctor`."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.diagnostics import run_doctor
from app.web.templates_engine import templates

router = APIRouter(tags=["doctor"])


@router.get("/doctor", response_class=HTMLResponse)
async def doctor_page(request: Request) -> HTMLResponse:
    """Render every diagnostic check as a pass/warn/fail row."""
    results = await run_doctor()
    summary = {
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "warn": sum(1 for r in results if r["status"] == "warn"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
    }
    return templates.TemplateResponse(
        request,
        "doctor.html",
        {
            "title": "Doctor",
            "active_nav": "settings",
            "results": results,
            "summary": summary,
        },
    )
