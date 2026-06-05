"""HTTP routes for one-click PII redaction preset packs.

Surfaces the :data:`app.redaction_packs.CATALOGUE` as a grid of cards
under ``/settings/redaction-packs`` plus a JSON view at
``/api/redaction-packs.json``. POSTing to
``/settings/redaction-packs/{pack_id}/install`` runs :func:`install_pack`
and redirects back with a ``?flash=...`` query string the template uses
to render a short banner — we deliberately avoid coupling this route to
the session layer just for a status message.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.redaction_packs import CATALOGUE, install_pack
from app.web.templates_engine import templates

router = APIRouter(tags=["redaction-packs"])

log = get_logger("persona.web.redaction_packs")


@router.get("/settings/redaction-packs", response_class=HTMLResponse)
async def redaction_packs_page(
    request: Request,
    flash: str | None = None,
) -> HTMLResponse:
    """Render the grid of preset packs.

    ``flash`` arrives from the install POST's redirect — kept as a plain
    query-string string so refreshing the page after dismissing the
    banner does not re-trigger anything. The template auto-escapes it.
    """
    return templates.TemplateResponse(
        request,
        "redaction_packs.html",
        {
            "title": "Готовые наборы PII-фильтров",
            "active_nav": "settings",
            "packs": CATALOGUE,
            "flash": flash,
        },
    )


@router.post("/settings/redaction-packs/{pack_id}/install")
async def redaction_packs_install(pack_id: str) -> RedirectResponse:
    """Install ``pack_id`` and redirect back with a flash query param."""
    try:
        report = await install_pack(pack_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    pack = CATALOGUE[pack_id]
    message = (
        f"Установлен набор «{pack['title']}»: "
        f"добавлено {report['inserted']}, уже было {report['skipped_duplicate']}."
    )
    return RedirectResponse(
        url=f"/settings/redaction-packs?flash={quote(message)}",
        status_code=303,
    )


@router.get("/api/redaction-packs.json")
async def redaction_packs_json() -> JSONResponse:
    """Return the full catalogue for programmatic use."""
    return JSONResponse({"packs": CATALOGUE})
