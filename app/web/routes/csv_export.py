"""CSV export of search results."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.search import search
from app.storage.db import get_connection

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/search.csv")
async def export_search_csv(
    q: str = Query(default=""),
    app_name: str | None = Query(default=None, alias="app"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> Response:
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    async with get_connection() as conn:
        hits = await search(
            conn,
            query=q,
            limit=10000,
            since=since_dt,
            until=until_dt,
            app_name=app_name,
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["captured_at", "app_name", "window_title", "snippet", "screenshot_id", "rank"])
    for h in hits:
        writer.writerow(
            [
                h.captured_at.isoformat(),
                h.app_name or "",
                h.window_title or "",
                _strip_marks(h.snippet),
                h.screenshot_id,
                round(h.rank, 4),
            ]
        )

    filename = f"persona-search-{q or 'all'}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _strip_marks(snippet: str) -> str:
    return snippet.replace("<mark>", "").replace("</mark>", "")


@router.get("/search.md")
async def export_search_markdown(
    q: str = Query(default=""),
    app_name: str | None = Query(default=None, alias="app"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> Response:
    """Render search results as Markdown — handy for pasting into a journal."""
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    async with get_connection() as conn:
        hits = await search(
            conn,
            query=q,
            limit=500,
            since=since_dt,
            until=until_dt,
            app_name=app_name,
        )

    lines: list[str] = [f"# Persona — search: `{q or 'all recent'}`", ""]
    if hits:
        for h in hits:
            ts = h.captured_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"## {ts} — {h.app_name or '—'}")
            if h.window_title:
                lines.append(f"_{h.window_title}_")
            lines.append("")
            if h.snippet:
                lines.append("> " + _strip_marks(h.snippet))
            lines.append(f"[screenshot #{h.screenshot_id}](/screenshot/{h.screenshot_id})")
            lines.append("")
    else:
        lines.append("No matches.")

    body = "\n".join(lines)
    filename = f"persona-search-{q or 'all'}.md"
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
