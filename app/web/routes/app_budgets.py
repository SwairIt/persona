"""Settings UI for per-app usage budget caps (v1.45).

Routes
------
GET  /settings/app-budgets               — table + add form + master toggle
POST /settings/app-budgets/new           — create or update one budget
POST /settings/app-budgets/{id}/delete   — drop one budget
POST /settings/app-budgets/{id}/toggle   — flip ``enabled`` on one budget
GET  /api/app-budgets/status.json        — today's tally (JSON)

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form. The status JSON endpoint exists for the optional
dashboard widget and the planned bell-widget badge.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.app_budgets import (
    check_today_status,
    delete_budget,
    list_budgets,
    toggle_budget,
    upsert_budget,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.web.app_budgets")

router = APIRouter(tags=["app-budgets"])

_SUGGESTION_LIMIT = 200


@router.get("/settings/app-budgets", response_class=HTMLResponse)
async def app_budgets_page(request: Request) -> HTMLResponse:
    """Render the budgets management page.

    Joins each configured budget with its current today-status entry
    so the table can show "used / cap" progress without an extra
    round-trip from the browser.
    """
    budgets = await list_budgets()
    status_entries = await check_today_status()
    status_by_id = {entry["id"]: entry for entry in status_entries}

    rows: list[dict[str, Any]] = []
    for b in budgets:
        st = status_by_id.get(b["id"])
        used = float(st["used_minutes"]) if st is not None else 0.0
        cap = int(b["daily_minutes_cap"])
        percent = (used / cap * 100.0) if cap > 0 else 0.0
        rows.append(
            {
                **b,
                "used_minutes": used,
                "percent": max(0.0, min(999.0, percent)),
                "breached": st is not None and st.get("breached_at") is not None,
            }
        )

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT app_name FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "ORDER BY app_name LIMIT ?",
            (_SUGGESTION_LIMIT,),
        )
        sugg_rows = await cursor.fetchall()
    existing = {b["app_name"] for b in budgets}
    suggestions = [
        str(row["app_name"])
        for row in sugg_rows
        if str(row["app_name"]) not in existing
    ]

    return templates.TemplateResponse(
        request,
        "app_budgets.html",
        {
            "title": "Бюджеты приложений",
            "active_nav": "settings",
            "rows": rows,
            "suggestions": suggestions,
        },
    )


@router.post("/settings/app-budgets/new")
async def app_budgets_create(
    app_name: str = Form(...),
    daily_minutes_cap: int = Form(...),
    alert_severity: str = Form("info"),
    enabled: str = Form("on"),
) -> RedirectResponse:
    """Add or update one budget, then 303 back to the table."""
    try:
        await upsert_budget(
            app_name=app_name,
            daily_minutes_cap=int(daily_minutes_cap),
            enabled=enabled.lower() in {"on", "1", "true", "yes"},
            alert_severity=alert_severity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/app-budgets", status_code=303)


@router.post("/settings/app-budgets/{budget_id}/delete")
async def app_budgets_delete(budget_id: int) -> RedirectResponse:
    """Drop one budget row, then 303 back to the table."""
    await delete_budget(budget_id)
    return RedirectResponse(url="/settings/app-budgets", status_code=303)


@router.post("/settings/app-budgets/{budget_id}/toggle")
async def app_budgets_toggle(budget_id: int) -> RedirectResponse:
    """Flip ``enabled`` on one budget row, then 303 back to the table."""
    await toggle_budget(budget_id)
    return RedirectResponse(url="/settings/app-budgets", status_code=303)


@router.get("/api/app-budgets/status.json")
async def app_budgets_status_json() -> JSONResponse:
    """Return today's per-budget tally as JSON.

    Shape::

        {"entries": [
            {"app_name": "Slack", "used_minutes": 18.3, "cap_minutes": 60,
             "percent": 30.5, "breached_at": null, "alert_severity": "info"},
            …
        ]}
    """
    entries = await check_today_status()
    return JSONResponse({"entries": entries})
