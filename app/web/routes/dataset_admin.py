"""T23 (2026-06-08) — /admin/dataset page + JSONL export.

Pages:
  GET  /admin/dataset           — stats dashboard + toggle + ratings
  POST /admin/dataset/toggle    — turn collection on/off
  POST /admin/dataset/rate      — set 👍/👎 on one row
  GET  /admin/dataset/export.jsonl — download for HuggingFace fine-tune
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.training import (
    is_enabled,
    iter_export_rows,
    set_enabled,
    set_rating,
    stats,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["dataset"])


@router.get("/admin/dataset", response_class=HTMLResponse, response_model=None)
async def dataset_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    info = await stats()
    enabled = await is_enabled()
    return templates.TemplateResponse(
        request,
        "dataset_admin.html",
        {
            "title": "PersonaAI датасет",
            "active_nav": "",
            "stats": info,
            "enabled": enabled,
        },
    )


@router.post("/admin/dataset/toggle", response_model=None)
async def dataset_toggle(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    enabled: Annotated[str, Form()] = "0",
) -> RedirectResponse:
    await set_enabled(enabled in ("1", "true", "on"))
    return RedirectResponse(url="/admin/dataset", status_code=303)


@router.post("/admin/dataset/rate", response_model=None)
async def dataset_rate(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    row_id: Annotated[int, Form()] = 0,
    rating: Annotated[int, Form()] = 0,
) -> RedirectResponse:
    if row_id <= 0:
        raise HTTPException(status_code=400, detail="row_id required")
    await set_rating(row_id, rating)
    return RedirectResponse(url="/admin/dataset", status_code=303)


@router.get("/admin/dataset/export.jsonl", response_model=None)
async def dataset_export(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    min_rating: int = Query(default=0, ge=-1, le=1),
    limit: int = Query(default=100_000, ge=1, le=1_000_000),
) -> StreamingResponse:
    """Stream the dataset as JSONL — HuggingFace-friendly format.

    Each line is a JSON object with ``messages`` (OpenAI chat format)
    and ``metadata``. Standard datasets like ``trl``'s SFTTrainer can
    load this directly via ``datasets.load_dataset('json', ...)``.
    """
    rows = await iter_export_rows(min_rating=min_rating, limit=limit)

    def serialise() -> str:
        for row in rows:
            yield (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(
        serialise(),
        media_type="application/jsonl",
        headers={
            "Content-Disposition": (
                f'attachment; filename="persona-dataset-min{min_rating}.jsonl"'
            ),
        },
    )
