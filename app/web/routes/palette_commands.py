"""HTTP layer for the colon-command palette mode.

Two endpoints — kept tiny on purpose; all the real work lives in
:mod:`app.palette_commands`:

* ``POST /api/palette/command`` — parse + execute, return a JSON
  envelope ``{ok, result, message}`` plus an optional ``redirect``
  field so ``:goto`` can navigate the browser without inventing a new
  HTTP status code.
* ``GET /api/palette/commands.json`` — return the catalogue (the same
  list :data:`app.palette_commands.CATALOGUE` exports) so the
  autocomplete JS can render hints without hardcoding the schema.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.palette_commands import (
    CATALOGUE,
    execute_command,
    parse_command,
)

log = get_logger("persona.palette_commands.routes")

router = APIRouter(tags=["palette-commands"])


class PaletteCommandPayload(BaseModel):
    """Request body for ``POST /api/palette/command``."""

    # Accept a generous max length so multi-word ``:note`` text fits;
    # the executor enforces a stricter cap.
    input: str = Field(..., min_length=1, max_length=8000)


@router.post("/api/palette/command", response_class=JSONResponse)
async def palette_command(payload: PaletteCommandPayload) -> JSONResponse:
    """Parse + execute a single colon-command and return a JSON envelope.

    Response shape:

    .. code-block:: json

        {
          "ok": true,
          "message": "screenshot 42 → pinned",
          "result": {"screenshot_id": 42, "tier": "pinned"},
          "redirect": ""
        }

    The handler never raises for user error — invalid input yields
    ``ok=False`` with a human-readable ``message`` instead, so the
    palette can render an inline hint without a try/catch in JS.
    """
    parsed = await parse_command(payload.input)
    executed = await execute_command(parsed)
    envelope: dict[str, Any] = {
        "ok": executed["ok"],
        "message": executed["message"],
        "result": executed["data"],
        "redirect": executed["redirect"],
    }
    log.info(
        "palette_commands.api.exec",
        command=parsed["command"],
        ok=executed["ok"],
    )
    return JSONResponse(envelope)


@router.get("/api/palette/commands.json", response_class=JSONResponse)
async def palette_commands_catalogue() -> JSONResponse:
    """Return the colon-command catalogue for JS autocomplete.

    Static — no DB hit. Returned as a list (not a dict) so the JS can
    iterate in the catalogue's intended display order.
    """
    log.debug("palette_commands.api.catalogue", count=len(CATALOGUE))
    return JSONResponse({"commands": list(CATALOGUE)})
