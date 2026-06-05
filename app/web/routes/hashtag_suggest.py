"""HTTP routes for the OCR hashtag auto-suggester.

Three endpoints:

* ``GET /api/screenshot/{shot_id}/hashtag-suggest.json`` — read-only JSON dump
  of :func:`app.hashtag_suggest.suggest_hashtags_for_shot`.
* ``POST /api/screenshot/{shot_id}/apply-hashtags`` — JSON body
  ``{"tags": [...]}``; inserts each tag into the ``tags`` table (idempotent on
  name) and attaches it to the shot via ``INSERT OR IGNORE`` (SQLite's
  ``ON CONFLICT DO NOTHING``). Returns ``{"applied": N}``.
* ``GET /widget/hashtag-suggest/{shot_id}`` — embeddable HTML fragment with the
  five candidate chips and a one-click Add button, ready to drop into the
  screenshot detail page via HTMX.

This module deliberately does NOT register itself with the FastAPI app in
:mod:`app.web.main` — the task spec forbids touching ``main.py``. Wire it up
with::

    from app.web.routes import hashtag_suggest as hashtag_suggest_routes
    app.include_router(hashtag_suggest_routes.router)
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.hashtag_suggest import suggest_hashtags_for_shot
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.hashtag_suggest.routes")

router = APIRouter(tags=["hashtag-suggest"])


# Hard upper bound on the number of tags one POST can attach. A larger cap
# would let a hostile client paper a single shot with thousands of rows; this
# stays well above the 5 the widget surfaces while still being a sane limit.
_MAX_APPLY: Final[int] = 32

# Tag-name slugifier: collapse runs of non-word characters to a single hyphen.
# Matches the slugifier in :mod:`app.phrase_autotag_suggest` so tags created
# through the two surfaces stay interchangeable.
_TAG_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"\W+", re.UNICODE)

# Maximum length of an individual tag *after* slugification. Defensive — the
# ``tags`` column is ``TEXT`` (unbounded) so the limit exists only to keep
# pathological OCR strings out of the index.
_TAG_MAX_LENGTH: Final[int] = 64


def _slugify(raw: str) -> str:
    """Lowercase, hyphenate, strip — mirrors the phrase-autotag slugifier."""
    lowered = raw.strip().lower()
    slug = _TAG_SLUG_RE.sub("-", lowered).strip("-")
    return slug[:_TAG_MAX_LENGTH]


async def _shot_exists(shot_id: int) -> bool:
    """Return ``True`` when ``shot_id`` matches a row in ``screenshots``."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM screenshots WHERE id = ? LIMIT 1",
            (shot_id,),
        )
        row = await cursor.fetchone()
    return row is not None


@router.get(
    "/api/screenshot/{shot_id}/hashtag-suggest.json",
    response_class=JSONResponse,
)
async def hashtag_suggest_json(shot_id: int) -> JSONResponse:
    """Return the top-5 hashtag candidates as JSON.

    404s when the screenshot id is unknown so clients can tell "no candidates
    for this shot" (empty list) apart from "this shot does not exist".
    """
    if not await _shot_exists(shot_id):
        raise HTTPException(status_code=404, detail=f"Screenshot not found: {shot_id}")
    payload = await suggest_hashtags_for_shot(shot_id)
    return JSONResponse(dict(payload))


@router.post(
    "/api/screenshot/{shot_id}/apply-hashtags",
    response_class=JSONResponse,
)
async def hashtag_apply(shot_id: int, request: Request) -> JSONResponse:
    """Persist user-selected tags as ``screenshot_tags`` rows.

    Body shape: ``{"tags": ["foo", "bar"]}``. Each string is slugified
    (lowercased, non-word chars collapsed to ``-``), empty results dropped,
    duplicates within the same request de-duped. The ``tags`` table is upserted
    by name (``INSERT OR IGNORE``); the join row goes in with another
    ``INSERT OR IGNORE`` so re-applying the same suggestion is a safe no-op.

    Returns ``{"applied": N, "skipped": M}`` where ``applied`` is the number of
    *new* join rows written and ``skipped`` covers both already-tagged shots
    and invalid inputs.
    """
    if not await _shot_exists(shot_id):
        raise HTTPException(status_code=404, detail=f"Screenshot not found: {shot_id}")

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    raw_tags = body.get("tags")
    if not isinstance(raw_tags, list):
        raise HTTPException(
            status_code=400,
            detail="body must contain a 'tags' list of strings",
        )
    if len(raw_tags) > _MAX_APPLY:
        raise HTTPException(
            status_code=400,
            detail=f"too many tags (max {_MAX_APPLY})",
        )

    cleaned: list[str] = []
    seen: set[str] = set()
    skipped_invalid = 0
    for entry in raw_tags:
        if not isinstance(entry, str):
            skipped_invalid += 1
            continue
        slug = _slugify(entry)
        if not slug or slug in seen:
            skipped_invalid += 1
            continue
        seen.add(slug)
        cleaned.append(slug)

    applied = 0
    skipped_existing = 0
    async with get_connection() as conn:
        for slug in cleaned:
            # Upsert the tag row by name. ``INSERT OR IGNORE`` is SQLite's
            # spelling of ``ON CONFLICT DO NOTHING`` — we then ``SELECT`` to
            # recover the canonical id whether the row was just inserted or
            # already existed. Both statements are parametrised; no formatting.
            await conn.execute(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)",
                (slug,),
            )
            cursor = await conn.execute(
                "SELECT id FROM tags WHERE name = ? LIMIT 1",
                (slug,),
            )
            row = await cursor.fetchone()
            if row is None:  # pragma: no cover — UNIQUE(name) guarantees a row
                continue
            tag_id = int(row["id"])
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO screenshot_tags (screenshot_id, tag_id) VALUES (?, ?)",
                (shot_id, tag_id),
            )
            if cursor.rowcount > 0:
                applied += 1
            else:
                skipped_existing += 1
        await conn.commit()

    log.info(
        "hashtag_suggest.applied",
        shot_id=shot_id,
        requested=len(raw_tags),
        applied=applied,
        skipped_existing=skipped_existing,
        skipped_invalid=skipped_invalid,
    )
    return JSONResponse(
        {
            "shot_id": shot_id,
            "applied": applied,
            "skipped_existing": skipped_existing,
            "skipped_invalid": skipped_invalid,
        }
    )


@router.get(
    "/widget/hashtag-suggest/{shot_id}",
    response_class=HTMLResponse,
)
async def hashtag_suggest_widget(shot_id: int, request: Request) -> HTMLResponse:
    """Render the five-chip widget for embedding via HTMX.

    Soft-fails when the shot does not exist: returns the same fragment with an
    empty candidates list rather than 404ing the HTMX swap, because the
    surrounding screenshot detail page is the only caller and it already
    handles the shot-missing case at the top level. The fragment renders an
    explicit "no candidates" line instead.
    """
    payload = await suggest_hashtags_for_shot(shot_id)
    return templates.TemplateResponse(
        request,
        "_hashtag_suggest.html",
        {
            "shot_id": shot_id,
            "candidates": payload["candidates"],
        },
    )


__all__ = ["router"]
