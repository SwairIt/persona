"""Admin page for the v0.62 per-app OCR language auto-detector.

Renders a table of every app with recent OCR text alongside the current
global Tesseract pack list and the recommendation produced by
:mod:`app.ocr.lang_autodetect`. A one-click "Apply" button per row
widens the ``ocr_languages`` kv_setting to the *union* of the current
selection and the row's recommended packs — never narrows it, so a
human-curated pack picked in :mod:`app.web.routes.ocr_languages` can
never be silently dropped by an auto-apply.

A bulk "Apply all" button does the same union over every row that has a
non-empty recommendation in one go.

The endpoint refuses to write packs that aren't actually installed on
the host (delegated to
:func:`app.ocr.languages.set_configured_languages`) so the OCR worker
never tries to invoke a missing language data file.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.ocr.lang_autodetect import recommend_languages_detailed
from app.ocr.languages import (
    get_configured_languages,
    get_installed_languages,
    set_configured_languages,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-admin"])
log = get_logger("persona.ocr.lang_autodetect")


def _union(current: list[str], extras: list[str]) -> list[str]:
    """Return the order-preserving union of ``current`` followed by ``extras``.

    Ordering matters because :func:`set_configured_languages` joins the
    list back into the ``+``-delimited string Tesseract reads
    left-to-right; preserving the existing order minimises diff churn
    on the visible "current pack string" between page renders.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for value in (*current, *extras):
        if value and value not in seen:
            seen.add(value)
            merged.append(value)
    return merged


@router.get("/admin/ocr-lang-autodetect", response_class=HTMLResponse)
async def lang_autodetect_page(request: Request) -> HTMLResponse:
    """Render the per-app recommendation table."""
    detail = await recommend_languages_detailed()
    configured = await get_configured_languages()
    installed = set(await get_installed_languages())
    configured_set = set(configured)

    rows: list[dict[str, Any]] = []
    for entry in detail:
        recommended = entry["recommended"]
        missing = [pack for pack in recommended if pack not in installed]
        already_covered = bool(recommended) and all(pack in configured_set for pack in recommended)
        rows.append(
            {
                "app_name": entry["app_name"],
                "shots_sampled": entry["shots_sampled"],
                "total_chars": entry["total_chars"],
                "script_percentages": entry["script_percentages"],
                "recommended": recommended,
                "missing_packs": missing,
                "already_covered": already_covered,
                "can_apply": bool(recommended) and not missing and not already_covered,
            }
        )

    appliable_apps = [row["app_name"] for row in rows if row["can_apply"]]

    return templates.TemplateResponse(
        request,
        "lang_autodetect.html",
        {
            "title": "OCR language auto-detect",
            "active_nav": "settings",
            "rows": rows,
            "configured": configured,
            "configured_string": "+".join(configured),
            "installed_count": len(installed),
            "appliable_count": len(appliable_apps),
        },
    )


@router.post("/admin/ocr-lang-autodetect/apply")
async def lang_autodetect_apply(request: Request) -> RedirectResponse:
    """Widen ``ocr_languages`` with the recommendation for one app.

    Reads the form field ``app_name`` (one ``<input type="hidden">`` per
    table row) and looks up that app's recommendation fresh — never
    trusts a recommended-packs list shipped from the client, so a
    tampered form can't enable an arbitrary pack the detector never
    suggested.
    """
    form = await request.form()
    raw_app = form.get("app_name")
    if raw_app is None:
        raise HTTPException(status_code=400, detail="Missing app_name")
    target_app = str(raw_app).strip()
    if not target_app:
        raise HTTPException(status_code=400, detail="Empty app_name")

    detail = await recommend_languages_detailed()
    match = next((row for row in detail if row["app_name"] == target_app), None)
    if match is None:
        log.info("ocr.lang_autodetect.apply.unknown_app", app=target_app)
        raise HTTPException(status_code=404, detail="App has no recommendation")

    recommended = match["recommended"]
    if not recommended:
        log.info("ocr.lang_autodetect.apply.no_recommendation", app=target_app)
        return RedirectResponse(url="/admin/ocr-lang-autodetect", status_code=303)

    current = await get_configured_languages()
    merged = _union(current, recommended)
    if merged == current:
        log.info("ocr.lang_autodetect.apply.already_covered", app=target_app)
        return RedirectResponse(url="/admin/ocr-lang-autodetect", status_code=303)

    try:
        await set_configured_languages(merged)
    except ValueError as exc:
        log.warning(
            "ocr.lang_autodetect.apply.rejected",
            app=target_app,
            recommended=recommended,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info(
        "ocr.lang_autodetect.apply.ok",
        app=target_app,
        added=[pack for pack in recommended if pack not in current],
        configured=merged,
    )
    return RedirectResponse(url="/admin/ocr-lang-autodetect", status_code=303)


@router.post("/admin/ocr-lang-autodetect/apply-all")
async def lang_autodetect_apply_all() -> RedirectResponse:
    """Widen ``ocr_languages`` with the union of every recommendation.

    Picks every row whose recommendation is fully installed on the host
    and merges those packs into the current selection in one write. Apps
    whose recommendation contains a missing pack are skipped (rather
    than aborting the whole batch) so a stale dataset can't block the
    well-formed rows.
    """
    detail = await recommend_languages_detailed()
    installed = set(await get_installed_languages())

    extras: list[str] = []
    seen: set[str] = set()
    apps_applied: list[str] = []
    apps_skipped: list[str] = []
    for entry in detail:
        recommended = entry["recommended"]
        if not recommended:
            continue
        if any(pack not in installed for pack in recommended):
            apps_skipped.append(entry["app_name"])
            continue
        added_for_this_app = False
        for pack in recommended:
            if pack not in seen:
                seen.add(pack)
                extras.append(pack)
                added_for_this_app = True
        if added_for_this_app or recommended:
            apps_applied.append(entry["app_name"])

    if not extras:
        log.info("ocr.lang_autodetect.apply_all.noop", apps=len(detail))
        return RedirectResponse(url="/admin/ocr-lang-autodetect", status_code=303)

    current = await get_configured_languages()
    merged = _union(current, extras)
    if merged == current:
        log.info("ocr.lang_autodetect.apply_all.already_covered", current=current)
        return RedirectResponse(url="/admin/ocr-lang-autodetect", status_code=303)

    try:
        await set_configured_languages(merged)
    except ValueError as exc:
        log.warning(
            "ocr.lang_autodetect.apply_all.rejected",
            extras=extras,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info(
        "ocr.lang_autodetect.apply_all.ok",
        apps_applied=len(apps_applied),
        apps_skipped=len(apps_skipped),
        added=[pack for pack in extras if pack not in current],
        configured=merged,
    )
    return RedirectResponse(url="/admin/ocr-lang-autodetect", status_code=303)
