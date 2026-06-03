"""Admin UI for the notes link checker (v0.77).

Three endpoints, all under ``/admin/notes-link-check``:

* ``GET  /admin/notes-link-check``      — render the trigger form +
  whatever the last run produced (or an "empty state" if it has never
  been run).
* ``POST /admin/notes-link-check/run``  — execute :func:`check_all_links`
  synchronously, persist the result blob to ``kv_settings`` under
  :data:`_KV_KEY`, then 303-redirect back to the GET above so a refresh
  doesn't re-trigger the run.
* ``GET  /admin/notes-link-check/last`` — same view as the main page,
  but explicit in the URL for bookmarking; both render the same template.

The result blob is stored as JSON in a single ``kv_settings`` row so we
don't need a new table for a feature that produces ~200 rows at most.
The payload is small (a few kB) and gets fully replaced on every run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.notes_link_checker import check_all_links
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["notes_link_checker"])
log = get_logger("persona.link_checker.routes")

# Single key under which the latest run's JSON blob is stored. Replaced
# wholesale on every run; we deliberately do not keep a history (the
# admin page only needs "what's broken right now").
_KV_KEY = "notes_link_check_last_result"

# Bounds for the ``max_links`` form input. The lower bound matches
# :func:`check_all_links`'s own guard (0 returns an empty list); the
# upper bound is generous enough for a manual one-off audit but small
# enough that a stray POST can't queue ten thousand HTTP requests.
_MIN_MAX_LINKS = 1
_MAX_MAX_LINKS = 2000
_DEFAULT_MAX_LINKS = 200

# Same idea for ``timeout``. We accept fractions (httpx accepts floats)
# but the form input is a plain integer slider — fractional values can
# still be POSTed by a curl power-user. 60s is the upper bound because
# the whole point of the cap is "page render shouldn't hang".
_MIN_TIMEOUT = 1.0
_MAX_TIMEOUT = 60.0
_DEFAULT_TIMEOUT = 5.0


def _clamp_int(raw: str, *, lo: int, hi: int, default: int) -> int:
    """Parse a form-supplied int and clamp to ``[lo, hi]``; fall back on parse error.

    The form widget itself uses ``min`` / ``max`` / ``step`` so a normal
    submission lands inside the bounds, but a hand-crafted POST could
    still send anything. We never raise — a bad value silently becomes
    the default, which beats 500'ing the page for an admin typo.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _clamp_float(raw: str, *, lo: float, hi: float, default: float) -> float:
    """Float version of :func:`_clamp_int` — same clamp-and-fallback policy."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


async def _load_last_result() -> dict[str, Any] | None:
    """Return the last persisted run, or ``None`` if there isn't one yet.

    A corrupt JSON blob (manual ``UPDATE kv_settings`` gone wrong) is
    treated the same as "no result" — better to render the empty state
    than 500 the admin page.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_KEY)
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("link_checker.last_result.corrupt", error=str(exc))
        return None
    if not isinstance(decoded, dict):
        log.warning("link_checker.last_result.not_dict")
        return None
    return decoded


def _render_template(
    request: Request,
    *,
    last_result: dict[str, Any] | None,
    just_ran: bool,
) -> HTMLResponse:
    """Shared renderer for both GET endpoints — keeps the context identical."""
    items: list[dict[str, Any]] = []
    ran_at: str | None = None
    total = 0
    bad = 0
    if last_result is not None:
        raw_items = last_result.get("items", [])
        if isinstance(raw_items, list):
            items = [it for it in raw_items if isinstance(it, dict)]
        total = int(last_result.get("total", len(items)))
        bad = int(last_result.get("bad", 0))
        ran_at_raw = last_result.get("ran_at")
        ran_at = str(ran_at_raw) if isinstance(ran_at_raw, str) else None

    return templates.TemplateResponse(
        request,
        "notes_link_check.html",
        {
            "title": "Notes link check",
            "active_nav": "settings",
            "items": items,
            "total": total,
            "bad": bad,
            "ran_at": ran_at,
            "just_ran": just_ran,
            "default_timeout": _DEFAULT_TIMEOUT,
            "default_max_links": _DEFAULT_MAX_LINKS,
            "min_timeout": _MIN_TIMEOUT,
            "max_timeout": _MAX_TIMEOUT,
            "min_max_links": _MIN_MAX_LINKS,
            "max_max_links": _MAX_MAX_LINKS,
        },
    )


@router.get("/admin/notes-link-check", response_class=HTMLResponse)
async def notes_link_check_page(request: Request) -> HTMLResponse:
    """Render the trigger form alongside the previous run's results."""
    last = await _load_last_result()
    return _render_template(request, last_result=last, just_ran=False)


@router.get("/admin/notes-link-check/last", response_class=HTMLResponse)
async def notes_link_check_last(request: Request) -> HTMLResponse:
    """Bookmarkable view of just the last result (same template as the main page)."""
    last = await _load_last_result()
    return _render_template(request, last_result=last, just_ran=False)


@router.post("/admin/notes-link-check/run")
async def notes_link_check_run(
    request: Request,
    timeout: str = Form(default=str(_DEFAULT_TIMEOUT)),
    max_links: str = Form(default=str(_DEFAULT_MAX_LINKS)),
) -> RedirectResponse:
    """Trigger a fresh run, persist the result, then 303 back to the GET.

    The HTTP work happens inline because :func:`check_all_links` is
    bounded by ``max_links`` (default 200) at ``timeout`` seconds each —
    worst case ~30s of wall-clock on a slow uplink, which the operator
    is explicitly waiting for. Moving this to a background worker would
    add a polling UI for very little payoff.
    """
    timeout_value = _clamp_float(
        timeout,
        lo=_MIN_TIMEOUT,
        hi=_MAX_TIMEOUT,
        default=_DEFAULT_TIMEOUT,
    )
    max_links_value = _clamp_int(
        max_links,
        lo=_MIN_MAX_LINKS,
        hi=_MAX_MAX_LINKS,
        default=_DEFAULT_MAX_LINKS,
    )

    results = await check_all_links(
        timeout=timeout_value,
        max_links=max_links_value,
    )

    # ``ran_at`` uses UTC + ``Z`` suffix so the template can format it
    # without re-parsing — the admin page is informational, not a
    # timeline, so we don't need a localised display.
    ran_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    bad = sum(1 for r in results if not 200 <= r["status"] < 300)
    payload: dict[str, Any] = {
        "ran_at": ran_at,
        "timeout": timeout_value,
        "max_links": max_links_value,
        "total": len(results),
        "bad": bad,
        # Convert the TypedDict rows into plain dicts so json.dumps is
        # happy without a default= hook. The shape is identical — we
        # just shed the type stamp.
        "items": [dict(r) for r in results],
    }

    async with get_connection() as conn:
        await set_kv(conn, _KV_KEY, json.dumps(payload, ensure_ascii=False))

    log.info(
        "link_checker.run.persisted",
        total=len(results),
        bad=bad,
        timeout=timeout_value,
        max_links=max_links_value,
    )
    # 303 because the trigger is a POST — refreshing the resulting GET
    # must not re-fire the check.
    return RedirectResponse(
        url="/admin/notes-link-check?ran=1",
        status_code=303,
    )
