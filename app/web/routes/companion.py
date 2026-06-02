"""Endpoint that ingests browser-extension tab samples."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from app.storage.db import get_connection
from app.storage.time import iso, parse_iso
from app.web.templates_engine import templates

router = APIRouter(tags=["companion"])


class TabSample(BaseModel):
    url: HttpUrl
    title: str = Field(default="", max_length=500)
    captured_at: str | None = None


@router.post("/api/companion/tab", response_class=JSONResponse)
async def ingest_tab(sample: TabSample) -> JSONResponse:
    url_str = str(sample.url)
    parsed = urlparse(url_str)
    domain = parsed.hostname or ""
    when_iso = sample.captured_at or iso(datetime.now())

    try:
        # Validate ISO format if user provided one.
        parse_iso(when_iso)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid captured_at") from exc

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO browser_tabs (url, title, domain, captured_at) VALUES (?, ?, ?, ?)",
            (url_str, sample.title or "", domain, when_iso),
        )
        await conn.commit()

    return JSONResponse({"ok": True, "domain": domain})


@router.get("/companion/tabs", response_class=HTMLResponse)
async def tabs_index(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, url, title, domain, captured_at FROM browser_tabs "
            "ORDER BY captured_at DESC LIMIT 200"
        )
        rows = await cursor.fetchall()
        tabs = [
            {
                "id": int(row["id"]),
                "url": str(row["url"]),
                "title": str(row["title"]),
                "domain": str(row["domain"]),
                "captured_at": parse_iso(str(row["captured_at"])),
            }
            for row in rows
        ]
        cursor = await conn.execute(
            "SELECT domain, COUNT(*) AS n FROM browser_tabs "
            "GROUP BY domain ORDER BY n DESC LIMIT 20"
        )
        top_domains = [
            {"domain": str(row["domain"]), "count": int(row["n"])} for row in await cursor.fetchall()
        ]
    return templates.TemplateResponse(
        request,
        "tabs.html",
        {
            "title": "Browser tabs",
            "active_nav": "companion",
            "tabs": tabs,
            "top_domains": top_domains,
        },
    )
