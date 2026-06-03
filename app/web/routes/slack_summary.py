"""HTTP route for the Slack-style daily summary.

``GET /export/slack-summary.txt?day=YYYY-MM-DD`` streams a
``text/plain`` document produced by
:func:`app.slack_summary.slack_style_summary` — a compact,
one-emoji-per-bullet recap suited for pasting into a Slack /
Mattermost / Discord channel without going through an LLM.

The intended workflow is::

    curl -s "http://localhost:8000/export/slack-summary.txt?day=2026-06-03" \\
        | pbcopy

so the response is served as an attachment with a date-stamped
filename, ``charset=utf-8``, and ``Cache-Control: no-store`` to keep
stale recaps out of browser caches.  ``?day=`` defaults to today's
local date so a parameter-less request still produces something useful.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.slack_summary import slack_style_summary

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.slack_summary")

router = APIRouter(prefix="/export", tags=["slack-summary"])


def _today_local_iso() -> str:
    """Return today's local date as ``YYYY-MM-DD``.

    Lifted into a tiny helper so the default-day branch is trivially
    overridable from tests (monkeypatch the symbol on this module) —
    mirrors :mod:`app.web.routes.ocr_txt_export`.
    """
    return datetime.now().astimezone().date().isoformat()


def _validate_day(day: str | None) -> str:
    """Validate ``?day=`` and return a canonical ``YYYY-MM-DD`` string.

    None / empty → today.  Anything else is parsed strictly with
    ``%Y-%m-%d`` so we reject near-misses (``2026/06/01``,
    ``20260601``) at the route boundary instead of letting them surface
    as a 500 from the SQL layer.
    """
    if not day:
        return _today_local_iso()
    try:
        parsed: date = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        msg = f"invalid day {day!r} (expected YYYY-MM-DD)"
        raise HTTPException(status_code=400, detail=msg) from exc
    return parsed.isoformat()


@router.get("/slack-summary.txt", response_model=None)
async def export_slack_summary(
    day: str | None = Query(default=None, description="Local day, YYYY-MM-DD; default today."),
) -> StreamingResponse:
    """Stream the Slack-style daily summary as ``text/plain``."""
    day_iso = _validate_day(day)

    try:
        body = await slack_style_summary(day_iso)
    except ValueError as exc:
        # Defensive — ``_validate_day`` already filtered bad formats,
        # but the summary helper may raise its own ValueError if a
        # future caller bypasses the validator.
        log.warning("slack_summary.route.bad_day", day=day_iso, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        log.exception("slack_summary.route.failed", day=day_iso)
        raise HTTPException(
            status_code=500,
            detail="Slack summary export failed",
        ) from None

    # Trailing newline so naive ``curl … > out.txt`` produces a
    # POSIX-conformant file (final line terminated).  The summary
    # itself deliberately has no trailing newline so the caller owns
    # the line-ending policy.
    payload = (body + "\n").encode("utf-8")
    filename = f"persona-slack-summary-{day_iso}.txt"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info("slack_summary.route.ok", day=day_iso, bytes=len(payload))

    return StreamingResponse(
        _iter(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
