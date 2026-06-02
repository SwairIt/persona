"""Ask-anything UI + endpoint — natural-language Q&A over past captures."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.llm import LLMNotConfigured, ask
from app.web.templates_engine import templates

router = APIRouter(tags=["qa"])


@router.get("/ask", response_class=HTMLResponse)
async def ask_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "ask.html",
        {"title": "Ask", "active_nav": "ask"},
    )


@router.post("/api/ask", response_class=JSONResponse)
async def ask_endpoint(question: str = Form(...), top_k: int = Form(default=10)) -> JSONResponse:
    if not question.strip():
        raise HTTPException(status_code=400, detail="Empty question")
    try:
        result = await ask(question, top_k=top_k)
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "answer": result.answer,
            "citations": result.citations,
            "used_screenshots": result.used_screenshots,
        }
    )
