"""Deeplink jump-to-time routes.

Exposes three endpoints, all backed by :func:`app.jump_to.find_closest_shot`:

* ``GET /goto?at=ISO[&window=MINUTES]`` — 303 redirect to
  ``/screenshot/{shot_id}`` if a shot is found in the window, otherwise
  renders ``jump_no_match.html`` with the original target so the user
  can widen the window.
* ``GET /api/goto.json?at=ISO[&window=MINUTES]`` — JSON variant of the
  same lookup, with ``redirect_url`` baked in for thin clients.
* ``GET /screenshot/{shot_id}/share-time`` — tiny standalone HTML page
  showing the canonical ``/goto?at=...`` deeplink for a given shot,
  with a one-click clipboard-copy button. Designed to be embedded in
  a popup or copied as-is.
"""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.jump_to import find_closest_shot
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.time import iso
from app.web.templates_engine import templates

router = APIRouter(tags=["jump-to-time"])
log = get_logger("persona.jump_to.routes")

# Hard caps so a malicious query string can't pin the DB. The default
# (60) is enough for "jump to the meeting I had at 3pm"; the upper
# bound covers "find the closest shot from yesterday" without going
# unbounded.
_DEFAULT_WINDOW = 60
_MAX_WINDOW = 24 * 60


@router.get("/goto", response_class=HTMLResponse, response_model=None)
async def goto(
    request: Request,
    at: str = Query(..., description="ISO 8601 target timestamp"),
    window: int = Query(
        default=_DEFAULT_WINDOW,
        ge=1,
        le=_MAX_WINDOW,
        description="Search window half-width in minutes",
    ),
) -> HTMLResponse | RedirectResponse:
    """Redirect to the closest screenshot, or render a no-match page."""
    try:
        match = await find_closest_shot(at, window_minutes=window)
    except ValueError as exc:
        log.info("jump_to.bad_request", at=at, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if match is not None:
        return RedirectResponse(
            url=f"/screenshot/{match['shot_id']}",
            status_code=303,
        )

    suggested_window = min(window * 2, _MAX_WINDOW)
    log.info(
        "jump_to.render_no_match",
        target=at,
        window=window,
        suggested_window=suggested_window,
    )
    return templates.TemplateResponse(
        request,
        "jump_no_match.html",
        {
            "title": "No match",
            "active_nav": "timeline",
            "target_at": at,
            "window_minutes": window,
            "suggested_window": suggested_window,
            "suggested_url": f"/goto?at={at}&window={suggested_window}",
        },
    )


@router.get("/api/goto.json")
async def goto_json(
    at: str = Query(..., description="ISO 8601 target timestamp"),
    window: int = Query(
        default=_DEFAULT_WINDOW,
        ge=1,
        le=_MAX_WINDOW,
        description="Search window half-width in minutes",
    ),
) -> JSONResponse:
    """JSON variant of :func:`goto` — returns the match dict + redirect_url."""
    try:
        match = await find_closest_shot(at, window_minutes=window)
    except ValueError as exc:
        log.info("jump_to.api_bad_request", at=at, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if match is None:
        payload: dict[str, Any] = {
            "match": None,
            "redirect_url": None,
            "target_at": at,
            "window_minutes": window,
        }
        return JSONResponse(payload, status_code=404)

    payload = dict(match)
    payload["redirect_url"] = f"/screenshot/{match['shot_id']}"
    payload["target_at"] = at
    payload["window_minutes"] = window
    return JSONResponse(payload)


@router.get("/screenshot/{shot_id}/share-time", response_class=HTMLResponse)
async def share_time(shot_id: int) -> HTMLResponse:
    """Render a tiny standalone page with the /goto?at=... deeplink + copy button.

    Intentionally does not extend ``base.html`` — the page is meant to
    be opened in a popup window or iframed from another tool, so we
    keep it self-contained and chrome-free.
    """
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    captured_iso = iso(shot.captured_at)
    deeplink = f"/goto?at={captured_iso}"
    safe_deeplink = escape(deeplink, quote=True)
    safe_captured = escape(captured_iso, quote=True)

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Share deeplink for screenshot #{shot_id}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0b0d12; color: #e5e7eb; margin: 0; padding: 24px;
          display: flex; flex-direction: column; gap: 16px; max-width: 640px; }}
  h1 {{ font-size: 1.1rem; margin: 0; color: #a5b4fc; }}
  p  {{ margin: 0; font-size: 0.85rem; color: #9ca3af; }}
  .row {{ display: flex; gap: 8px; align-items: stretch; }}
  input[type=text] {{ flex: 1; padding: 8px 10px; border-radius: 6px;
                      border: 1px solid #374151; background: #111827;
                      color: #e5e7eb; font-family: ui-monospace, monospace;
                      font-size: 0.85rem; }}
  button {{ padding: 8px 14px; border-radius: 6px; border: 0;
            background: #6366f1; color: white; font-weight: 600;
            cursor: pointer; font-size: 0.85rem; }}
  button:disabled {{ background: #10b981; }}
  .meta {{ font-family: ui-monospace, monospace; font-size: 0.75rem; color: #6b7280; }}
</style>
</head>
<body>
  <h1>Deeplink for screenshot #{shot_id}</h1>
  <p>Anyone with this URL on the same Persona instance jumps straight to this moment.</p>
  <div class="row">
    <input id="deeplink" type="text" readonly value="{safe_deeplink}">
    <button id="copyBtn" type="button">Copy</button>
  </div>
  <p class="meta">captured_at = {safe_captured}</p>
  <script>
    (function () {{
      var btn = document.getElementById('copyBtn');
      var input = document.getElementById('deeplink');
      btn.addEventListener('click', function () {{
        var value = new URL(input.value, window.location.origin).toString();
        var done = function () {{
          var original = btn.textContent;
          btn.textContent = 'Copied';
          btn.disabled = true;
          setTimeout(function () {{
            btn.textContent = original;
            btn.disabled = false;
          }}, 1500);
        }};
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(value).then(done, function () {{
            input.select();
            document.execCommand('copy');
            done();
          }});
        }} else {{
          input.select();
          document.execCommand('copy');
          done();
        }}
      }});
    }})();
  </script>
</body>
</html>"""
    return HTMLResponse(body)
