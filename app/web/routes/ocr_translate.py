"""HTTP surface for the OCR-translate feature.

Two surfaces (mirrors :mod:`app.web.routes.ocr_vision`):

* ``POST /api/screenshot/{id}/translate`` — form field ``target_lang``;
  triggers a translation and returns JSON with the status + text.
* ``GET  /admin/ocr-translate`` — admin page listing screenshots whose
  OCR text exists but has not yet been translated, with a bulk
  "translate all shown" button capped at :data:`PAGE_LIMIT` rows.

The admin page intentionally only surfaces rows where
``ocr_text_translated IS NULL`` so we never silently overwrite an
existing translation just because the user clicked "bulk translate"
again. Each row keeps its own ``data-shot-id`` so the per-row button
fires independently of the bulk button — both go through the same
JSON endpoint, just sequentially in the bulk case.

No HTMX / Alpine dependency on this page — plain ``fetch`` + a tiny
DOM update keeps the surface area minimal and matches the v0.55
ocr-vision admin page's approach.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.llm.client import LLMNotConfigured, make_client
from app.llm.ocr_translate import translate_shot
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-translate"])
log = get_logger("persona.ocr.translate")

# Bulk translation cap. Each call is a paid completion against the
# user's BYO key, so surfacing thousands of rows and letting the user
# fire them all from one click would be financially hostile. 50 keeps
# bulk runs feasible without turning into a denial-of-wallet button.
PAGE_LIMIT: int = 50

# Default target language for the admin form input. ``ru`` matches
# Persona's primary developer audience and is what the field is
# pre-populated with — the user can edit it before bulk-running.
_DEFAULT_TARGET_LANG: str = "ru"


async def _list_untranslated(limit: int) -> list[dict[str, Any]]:
    """Return rows whose ``ocr_text_translated IS NULL`` and OCR text exists.

    We deliberately exclude rows with empty / NULL ``ocr_text`` because
    there's nothing to translate — they'd just return ``no_source`` and
    waste a row in the admin table. The list is capped at ``limit`` and
    ordered newest-first so the most recent shots appear at the top.
    """
    sql = (
        "SELECT id, captured_at, app_name, window_title, thumbnail_path, "
        "       ocr_text "
        "FROM screenshots "
        "WHERE ocr_text_translated IS NULL "
        "  AND ocr_text IS NOT NULL "
        "  AND TRIM(ocr_text) <> '' "
        "ORDER BY captured_at DESC "
        "LIMIT ?"
    )
    async with get_connection() as conn:
        cursor = await conn.execute(sql, (limit,))
        rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "captured_at": str(row["captured_at"]),
                "app_name": (None if row["app_name"] is None else str(row["app_name"])),
                "window_title": (None if row["window_title"] is None else str(row["window_title"])),
                "thumbnail_path": (
                    None if row["thumbnail_path"] is None else str(row["thumbnail_path"])
                ),
                "ocr_text_preview": _preview(str(row["ocr_text"]), 160),
            }
        )
    return out


async def _count_untranslated() -> int:
    """Return the total number of untranslated shots (for the header)."""
    sql = (
        "SELECT COUNT(*) AS n "
        "FROM screenshots "
        "WHERE ocr_text_translated IS NULL "
        "  AND ocr_text IS NOT NULL "
        "  AND TRIM(ocr_text) <> ''"
    )
    async with get_connection() as conn:
        cursor = await conn.execute(sql)
        row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


def _preview(text: str, max_chars: int) -> str:
    """Collapse whitespace + truncate ``text`` for the admin preview cell."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 1].rstrip() + "…"


def _check_llm_ready() -> tuple[bool, str]:
    """Return ``(ready, provider_label)`` for the header status pill.

    We don't need to know which provider is configured for the route
    to function — :func:`translate_shot` makes its own client — but
    the admin template wants to show "Ready" vs "Missing key" so the
    user knows whether the bulk button will work before they click it.
    """
    try:
        client = make_client()
    except LLMNotConfigured:
        return False, "(unset)"
    return True, str(client.provider)


@router.post(
    "/api/screenshot/{screenshot_id}/translate",
    response_class=JSONResponse,
)
async def ocr_translate_trigger(
    screenshot_id: int,
    target_lang: str = Form(...),
) -> JSONResponse:
    """Trigger a translation for ``screenshot_id`` into ``target_lang``.

    The underlying :func:`translate_shot` helper is tolerant — it
    returns a status string rather than raising on configuration /
    network / data problems — so this endpoint passes the result
    straight through to the client and tacks on ``screenshot_id`` for
    the JS layer.
    """
    result = await translate_shot(screenshot_id, target_lang)
    log.info(
        "ocr.translate.route.trigger",
        shot_id=screenshot_id,
        status=result["status"],
        target_lang=result["target_lang"],
        chars=len(result["text"]),
    )
    return JSONResponse(
        {
            "screenshot_id": screenshot_id,
            "status": result["status"],
            "text": result["text"],
            "target_lang": result["target_lang"],
        }
    )


@router.get("/admin/ocr-translate", response_class=HTMLResponse)
async def ocr_translate_admin_page(request: Request) -> HTMLResponse:
    """Render the admin dashboard listing untranslated screenshots."""
    rows = await _list_untranslated(PAGE_LIMIT)
    total = await _count_untranslated()
    ready, provider = _check_llm_ready()
    return templates.TemplateResponse(
        request,
        "ocr_translate_admin.html",
        {
            "title": "OCR translate",
            "active_nav": "settings",
            "rows": rows,
            "total": total,
            "shown": len(rows),
            "page_limit": PAGE_LIMIT,
            "default_target_lang": _DEFAULT_TARGET_LANG,
            "feature_ready": ready,
            "provider": provider,
        },
    )
