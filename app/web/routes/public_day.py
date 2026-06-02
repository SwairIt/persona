"""Public-day opt-in HTTP routes.

Two surfaces share this module:

* ``/public/day/{slug}`` — unauthenticated, minimal-chrome render of a
  day's screenshots + notes for sharing with a public audience. Strips
  annotations, private/confidential-tagged shots, and runs OCR text
  through :func:`app.redaction.apply_redaction` so the same masking
  policy that protects the searchable index also protects the
  externally visible captions.
* ``/admin/public-days`` — admin list + publish form, plus
  ``/admin/public-days/{day}/unpublish``. These sit behind the same
  auth as the rest of the admin UI (no extra wiring here — they live
  on the existing router).

Slug validation, persistence, and slug-uniqueness all live in
:mod:`app.public_day`. The route module just translates HTTP <-> the
helpers and renders the two templates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import public_day as public_day_store
from app.logging_setup import get_logger
from app.redaction import apply_redaction
from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.storage.tags import get_tags_for_many
from app.web.routes.thumbnails import thumbnail_url
from app.web.templates_engine import templates

log = get_logger("persona.public_day")

router = APIRouter(tags=["public-day"])

# Tag names (lowercase) that exclude a screenshot from the public view.
# Matched case-insensitively against ``tags.name`` so an operator can
# spell them either way in the UI without surprises.
_SENSITIVE_TAGS: Final[frozenset[str]] = frozenset({"private", "confidential"})

# Cap on screenshots per public-day render. A day that captured every
# few seconds can produce thousands of rows; we keep the page light by
# stopping at this many — same ceiling the day scrubber uses.
_MAX_SHOTS_PER_DAY: Final[int] = 5_000


def _day_bounds_utc(day_iso: str) -> tuple[datetime, datetime]:
    """Translate a local ``YYYY-MM-DD`` into the half-open UTC window
    ``[since, until)`` that ``list_screenshots`` expects."""
    parsed = datetime.strptime(day_iso, "%Y-%m-%d").date()
    tz = datetime.now().astimezone().tzinfo
    since_local = datetime(parsed.year, parsed.month, parsed.day, tzinfo=tz)
    until_local = since_local + timedelta(days=1)
    return since_local.astimezone(UTC), until_local.astimezone(UTC)


def _shot_is_sensitive(tags: list[dict[str, Any]]) -> bool:
    """Return True if any tag name (lowercased) is in the sensitive set."""
    for tag in tags:
        name = str(tag.get("name", "")).strip().lower()
        if name in _SENSITIVE_TAGS:
            return True
    return False


async def _load_public_day_shots(day_iso: str) -> list[dict[str, Any]]:
    """Fetch a day's screenshots, filter sensitive ones, mask OCR text.

    Returns a list of plain dicts (not Screenshot pydantic instances) so
    the template can read ``shot.thumbnail_url`` and ``shot.caption``
    without importing the model. Order is chronological — earliest
    first — so the public reader sees the day unfold as it happened.
    """
    since_dt, until_dt = _day_bounds_utc(day_iso)
    async with get_connection() as conn:
        shots = await list_screenshots(
            conn,
            limit=_MAX_SHOTS_PER_DAY,
            since=since_dt,
            until=until_dt,
        )
        if not shots:
            return []
        # Drop private-vault rows up front — they should never reach a
        # public surface regardless of tagging.
        shots = [s for s in shots if not s.is_private]
        tags_by_sid = await get_tags_for_many(conn, [s.id for s in shots])
        notes_by_sid = await _load_notes_map(conn, [s.id for s in shots])

    shots.sort(key=lambda s: s.captured_at)

    out: list[dict[str, Any]] = []
    for shot in shots:
        sid = int(shot.id)
        tags = tags_by_sid.get(sid, [])
        if _shot_is_sensitive(tags):
            continue
        thumb = thumbnail_url(shot.thumbnail_path) if shot.thumbnail_path else None
        # Mask OCR text via the same rules the indexer uses. We use the
        # OCR text as a caption when no per-shot note exists.
        raw_ocr = shot.ocr_text or ""
        masked_ocr, _ = await apply_redaction(raw_ocr)
        note_body = notes_by_sid.get(sid)
        masked_note: str | None = None
        if note_body:
            masked_note_text, _ = await apply_redaction(note_body)
            masked_note = masked_note_text
        out.append(
            {
                "id": sid,
                "captured_at": shot.captured_at,
                "app_name": shot.app_name,
                "thumbnail_url": thumb,
                "caption": masked_note,
                "ocr_excerpt": _excerpt(masked_ocr),
                "tags": [
                    str(t["name"])
                    for t in tags
                    if str(t.get("name", "")).strip().lower() not in _SENSITIVE_TAGS
                ],
            }
        )
    return out


async def _load_notes_map(
    conn: Any,
    shot_ids: list[int],
) -> dict[int, str]:
    """Bulk-fetch ``screenshot_notes.body`` for the given ids."""
    if not shot_ids:
        return {}
    placeholders = ",".join("?" * len(shot_ids))
    cursor = await conn.execute(
        f"SELECT screenshot_id, body FROM screenshot_notes WHERE screenshot_id IN ({placeholders})",  # noqa: S608 — placeholders are only "?"
        shot_ids,
    )
    rows = await cursor.fetchall()
    return {int(row["screenshot_id"]): str(row["body"]) for row in rows}


def _excerpt(text: str, *, limit: int = 240) -> str:
    """Return a one-line excerpt of ``text`` capped at ``limit`` chars."""
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@router.get("/public/day/{slug}", response_class=HTMLResponse)
async def public_day_view(request: Request, slug: str) -> HTMLResponse:
    """Render the public-day page for ``slug``.

    A 404 is returned for unknown slugs *and* for malformed slugs — we
    deliberately do not leak which is which, since the URL is meant to
    be guessable only when the operator hands it out.
    """
    row = await public_day_store.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="Public day not found")
    day_iso = str(row["day"])
    shots = await _load_public_day_shots(day_iso)
    log.info(
        "public_day.view",
        slug=slug,
        day=day_iso,
        shots=len(shots),
    )
    return templates.TemplateResponse(
        request,
        "public_day.html",
        {
            "title": str(row["title"]),
            "day": day_iso,
            "slug": slug,
            "page_title": str(row["title"]),
            "blurb": row["blurb"],
            "published_at": str(row["published_at"]),
            "shots": shots,
            "shot_count": len(shots),
        },
    )


# ---------------------------------------------------------------------------
# Admin surface
# ---------------------------------------------------------------------------


@router.get("/admin/public-days", response_class=HTMLResponse)
async def admin_public_days(request: Request) -> HTMLResponse:
    """List currently-published days + offer a form to publish a new one."""
    rows = await public_day_store.list_published()
    return templates.TemplateResponse(
        request,
        "public_day_admin.html",
        {
            "title": "Public days",
            "active_nav": "settings",
            "rows": rows,
        },
    )


@router.post("/admin/public-days")
async def admin_public_days_create(
    day: str = Form(...),
    slug: str = Form(...),
    title: str = Form(...),
    blurb: str | None = Form(None),
) -> RedirectResponse:
    """Publish a day. Validation errors surface as 400 with the helper's
    message so the admin can correct the form."""
    try:
        await public_day_store.publish(day, slug, title, blurb)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # The most common failure here is a slug-uniqueness collision —
        # turn it into a 400 so the admin retries rather than 500ing.
        log.warning("public_day.publish_failed", error=str(exc), day=day, slug=slug)
        raise HTTPException(
            status_code=400,
            detail=f"Could not publish day: {exc}",
        ) from exc
    return RedirectResponse(url="/admin/public-days", status_code=303)


@router.post("/admin/public-days/{day}/unpublish")
async def admin_public_days_unpublish(day: str) -> RedirectResponse:
    """Remove the public-day row for ``day``."""
    try:
        await public_day_store.unpublish(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/admin/public-days", status_code=303)


__all__ = ["router"]
