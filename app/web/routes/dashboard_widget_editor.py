"""Dashboard grid editor — HTML page + JSON layout API (v1.37).

Three endpoints, three callers:

* ``GET  /settings/dashboard-grid`` — Jinja-rendered editor. Two
  columns: Active (drag-drop sortable) and Available (click to add).
  Reads :func:`app.dashboard_widgets.list_slots` for the user's saved
  layout and walks :data:`app.dashboard_widgets.WIDGET_CATALOGUE` to
  fill the Available column with anything not already claimed by a
  slot.
* ``POST /settings/dashboard-grid`` — JSON body
  ``{"widgets": [{"key": "...", "enabled": true, "slot_id": int?}, …]}``
  describing the desired full layout in order. The bulk save is
  transactional and removes any pre-existing slot whose id is not in
  the payload (see :func:`app.dashboard_widgets.upsert_widgets`).
* ``GET  /api/dashboard/grid.json`` — read-only contract for the
  /dashboard renderer (and any future external surface). Returns
  ``{"widgets": [{...}], "catalogue": [{...}]}`` so callers can render
  the active list and resolve human titles in a single round-trip.

Out of scope deliberately:

* No partial / row-level update — the editor always POSTs the full
  list. That keeps reorder + add + remove + toggle a single round-trip
  and means the server never has to reason about "phantom" slots
  whose id the client never knew about.
* No CSRF token — Persona is single-user on localhost; the rest of
  /settings is the same shape (see :mod:`dashboard_tiles`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.dashboard_widgets import (
    WIDGET_CATALOGUE,
    list_active_widgets,
    list_slots,
    upsert_widgets,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.dashboard_widgets.editor")

router = APIRouter(tags=["dashboard"])


def _catalogue_payload() -> list[dict[str, Any]]:
    """Catalogue rows in the renderer's order — title/description/key.

    Sorted by ``default_position`` so the Available column shows
    widgets in a stable, predictable order (otherwise dict-insertion
    order leaks through and the layout's "future" widgets would
    cluster at the bottom regardless of who got added later).
    """
    rows: list[dict[str, Any]] = [
        {
            "key": key,
            "title": meta["title"],
            "description": meta["description"],
            "default_position": meta["default_position"],
        }
        for key, meta in WIDGET_CATALOGUE.items()
    ]
    rows.sort(key=lambda row: int(row["default_position"]))
    return rows


@router.get("/settings/dashboard-grid", response_class=HTMLResponse)
async def dashboard_grid_editor(request: Request) -> HTMLResponse:
    """Render the drag-drop editor with Active + Available columns."""
    slots = await list_slots()

    # Map slot rows to the template-side shape: title/description come
    # from the catalogue, but a slot whose key has been retired from
    # the catalogue (older row, code rolled back) still renders with a
    # safe fallback so the editor never 500s on a stale DB.
    active: list[dict[str, Any]] = []
    claimed_keys: set[str] = set()
    for slot in slots:
        key = slot["widget_key"]
        meta = WIDGET_CATALOGUE.get(key)
        active.append(
            {
                "slot_id": slot["slot_id"],
                "key": key,
                "title": meta["title"] if meta else key,
                "description": meta["description"] if meta else "(unknown widget)",
                "enabled": slot["enabled"],
                "known": meta is not None,
            }
        )
        claimed_keys.add(key)

    # Available = catalogue minus everything already represented by a
    # slot (regardless of enabled state). That way a user can't end up
    # with two slots for the same widget — toggling visibility happens
    # inside the Active column.
    available = [
        row for row in _catalogue_payload() if row["key"] not in claimed_keys
    ]

    log.info(
        "dashboard_widgets.editor.render",
        active=len(active),
        available=len(available),
    )

    return templates.TemplateResponse(
        request,
        "dashboard_widget_editor.html",
        {
            "title": "Сетка дашборда",
            "active_nav": "settings",
            "active_widgets": active,
            "available_widgets": available,
        },
    )


@router.post("/settings/dashboard-grid", response_class=JSONResponse)
async def dashboard_grid_save(request: Request) -> JSONResponse:
    """Persist the new layout from the editor.

    Body shape (validated row-by-row in the service layer)::

        {
          "widgets": [
            {"key": "today_vs_average", "enabled": true, "slot_id": 1},
            {"key": "voice_note",       "enabled": false, "slot_id": 4},
            {"key": "entity_cloud",     "enabled": true}
          ]
        }

    ``position`` is implicit by array index — the editor controls the
    order client-side via SortableJS and the server just preserves it.
    A new widget (picker → active) has no ``slot_id`` and is INSERTed.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("dashboard_widgets.editor.bad_json", error=str(exc))
        raise HTTPException(status_code=400, detail="invalid_json") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")

    widgets_raw = payload.get("widgets")
    if not isinstance(widgets_raw, list):
        raise HTTPException(status_code=400, detail="widgets_must_be_list")

    updates: list[dict[str, Any]] = []
    for index, entry in enumerate(widgets_raw):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=400,
                detail=f"widgets[{index}] must be an object",
            )
        key = entry.get("key")
        enabled = entry.get("enabled", True)
        slot_id = entry.get("slot_id")
        update: dict[str, Any] = {
            "widget_key": key,
            "position": index,
            "enabled": enabled,
        }
        if slot_id is not None:
            update["slot_id"] = slot_id
        if "options_json" in entry:
            update["options_json"] = entry["options_json"]
        updates.append(update)

    try:
        total = await upsert_widgets(updates)
    except ValueError as exc:
        # Service-layer validation failures (unknown key, malformed
        # options JSON, bad position) → 400. Anything else is a real
        # 500 we want to see in the logs.
        log.warning("dashboard_widgets.editor.validation_failed", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info("dashboard_widgets.editor.saved", count=len(updates), total=total)
    return JSONResponse({"ok": True, "count": len(updates), "total": total})


@router.get("/api/dashboard/grid.json", response_class=JSONResponse)
async def dashboard_grid_json() -> JSONResponse:
    """Return the current active layout for the dashboard renderer.

    Shape::

        {
          "widgets":   [{"slot_id", "key", "position", "title", ...}],
          "catalogue": [{"key", "title", "description", ...}]
        }

    The catalogue is included so a client (the live dashboard, an
    external dashboard mirror, the editor's "Reset" button in a
    future tick) can resolve titles without a second round-trip.
    """
    active = await list_active_widgets()

    widgets_payload: list[dict[str, Any]] = []
    for row in active:
        widgets_payload.append(
            {
                "slot_id": row["slot_id"],
                "key": row["widget_key"],
                "position": row["position"],
                "enabled": row["enabled"],
                "title": row.get("title", row["widget_key"]),
                "description": row.get("description", ""),
                "options": row.get("options"),
                "updated_at": row["updated_at"],
            }
        )

    return JSONResponse(
        {
            "widgets": widgets_payload,
            "catalogue": _catalogue_payload(),
        }
    )
