"""Vim-style colon-command mode for the command palette (v1.x).

When the user types ``:`` as the first character in the Cmd+K palette
the UI flips from fuzzy-search mode to *command mode*: every keystroke
is parsed as a single-line action (``:pin 42``, ``:tag 17 work``,
``:goto 2026-06-05T09:00``, ``:theme dark``). The catalogue below is
the single source of truth — JS autocomplete consumes it via
``GET /api/palette/commands.json`` and the executor in
:func:`execute_command` dispatches against the same ``command`` keys.

Design choices:

* The executor talks **directly** to the storage layer (DB / kv) so
  there's no HTTP round-trip back through ``/api/screenshots/{id}/pin``
  etc. — that keeps the action atomic, avoids re-authentication, and
  keeps the route count down.
* ``handler_url`` in :data:`CATALOGUE` is informational only — surfaced
  to the autocomplete JS so power users can see which endpoint the
  command *would* hit in REST terms, but never actually fetched here.
* Errors are returned as dicts (``valid=False`` + ``error="…"``),
  never raised — the palette must render an inline hint instead of
  500-ing the UI.

All SQL is parametrised; no ``f"…{user_input}…"`` interpolation.
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.notes import insert_inbox_note
from app.storage.repository import get_kv, get_screenshot, set_kv
from app.storage.tags import create_tag, tag_screenshot
from app.storage.tiers import pin_screenshot, unpin_screenshot

log = get_logger("persona.palette_commands")


# ---------------------------------------------------------------------------
# Catalogue — single source of truth, consumed by JS autocomplete too
# ---------------------------------------------------------------------------


class CommandSpec(TypedDict):
    """One row in :data:`CATALOGUE`."""

    name: str
    syntax: str
    description: str
    handler_url: str


CATALOGUE: Final[list[CommandSpec]] = [
    {
        "name": "pin",
        "syntax": ":pin <id>",
        "description": "Pin a screenshot so it survives the tier sweep.",
        "handler_url": "/api/screenshot/{id}/pin",
    },
    {
        "name": "unpin",
        "syntax": ":unpin <id>",
        "description": "Unpin a screenshot (back to the hot tier).",
        "handler_url": "/api/screenshot/{id}/unpin",
    },
    {
        "name": "tag",
        "syntax": ":tag <id> <tag>",
        "description": "Attach a tag to a screenshot (creates the tag if new).",
        "handler_url": "/api/screenshot/{id}/tag",
    },
    {
        "name": "note",
        "syntax": ":note <text>",
        "description": "Create a standalone inbox note.",
        "handler_url": "/api/notes",
    },
    {
        "name": "goto",
        "syntax": ":goto <ISO>",
        "description": "Redirect to /goto?at=<ISO>.",
        "handler_url": "/goto?at={iso}",
    },
    {
        "name": "pause",
        "syntax": ":pause",
        "description": "Pause screen capture (sets capture_screens_disabled=1).",
        "handler_url": "kv:capture_screens_disabled=1",
    },
    {
        "name": "resume",
        "syntax": ":resume",
        "description": "Resume screen capture (sets capture_screens_disabled=0).",
        "handler_url": "kv:capture_screens_disabled=0",
    },
    {
        "name": "mic",
        "syntax": ":mic on|off",
        "description": "Toggle the live microphone pause flag.",
        "handler_url": "kv:audio_capture_paused_live",
    },
    {
        "name": "theme",
        "syntax": ":theme dark|light|auto",
        "description": "Switch the UI theme.",
        "handler_url": "kv:theme",
    },
    {
        "name": "help",
        "syntax": ":help",
        "description": "List every colon-command (this catalogue).",
        "handler_url": "",
    },
]

_NAMES: Final[frozenset[str]] = frozenset(spec["name"] for spec in CATALOGUE)
_THEME_VALID: Final[frozenset[str]] = frozenset({"dark", "light", "auto"})
_MIC_VALID: Final[frozenset[str]] = frozenset({"on", "off"})

# Maximum length of a single colon-command input. Anything beyond is a
# misclick or paste of a whole note — reject early instead of stuffing
# megabytes into the parser.
_MAX_INPUT_LEN: Final[int] = 4000


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class ParsedCommand(TypedDict):
    """Result of :func:`parse_command`. Always returned, never raised."""

    command: str
    args: list[str]
    valid: bool
    error: str


async def parse_command(raw: str) -> ParsedCommand:  # noqa: PLR0911 — early-return chain on validation failures stays flatter than nested ifs
    """Parse a single-line colon-command into a ``ParsedCommand`` dict.

    Recognised shapes:

    * ``:pin 42`` → ``{"command": "pin", "args": ["42"], "valid": True}``
    * ``:note buy milk`` → ``args = ["buy milk"]`` (rest-of-line joined)
    * ``:tag 17 work`` → ``args = ["17", "work"]``
    * ``:pause`` / ``:resume`` / ``:help`` → ``args = []``

    Validation here is *shape-only* — does the command exist, did the
    user supply the right number of positional tokens? Semantic checks
    (does the screenshot exist? is the theme value one of the three
    valid strings?) happen inside :func:`execute_command` so the parser
    stays pure.
    """
    text = raw.strip()
    if not text:
        return {"command": "", "args": [], "valid": False, "error": "empty input"}
    if len(text) > _MAX_INPUT_LEN:
        return {
            "command": "",
            "args": [],
            "valid": False,
            "error": f"input exceeds {_MAX_INPUT_LEN} chars",
        }
    if not text.startswith(":"):
        return {
            "command": "",
            "args": [],
            "valid": False,
            "error": "colon-commands must start with ':'",
        }
    body = text[1:].strip()
    if not body:
        return {"command": "", "args": [], "valid": False, "error": "missing command name"}

    # ``:note`` is the only command that takes free-form text as a
    # single argument — splitting on whitespace would shred multi-word
    # notes into a list of one-word fragments. Special-case it here.
    head, _, tail = body.partition(" ")
    name = head.lower()
    if name not in _NAMES:
        return {
            "command": name,
            "args": [],
            "valid": False,
            "error": f"unknown command :{name}",
        }
    if name == "note":
        note_body = tail.strip()
        if not note_body:
            return {
                "command": name,
                "args": [],
                "valid": False,
                "error": ":note requires text",
            }
        return {"command": name, "args": [note_body], "valid": True, "error": ""}

    args = [tok for tok in tail.split() if tok]
    return {"command": name, "args": args, "valid": True, "error": ""}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class ExecutionResult(TypedDict):
    """Result of :func:`execute_command`. Always returned, never raised."""

    ok: bool
    message: str
    redirect: str
    data: dict[str, Any]


def _empty_result() -> ExecutionResult:
    return {"ok": False, "message": "", "redirect": "", "data": {}}


def _parse_int(token: str) -> int | None:
    try:
        return int(token, 10)
    except (TypeError, ValueError):
        return None


async def execute_command(parsed: ParsedCommand) -> ExecutionResult:  # noqa: PLR0911 — dispatcher over a fixed-size catalogue; a dict lookup would just push the returns into the helpers
    """Dispatch a parsed command to the right storage-layer helper.

    Returns a structured :class:`ExecutionResult` — ``ok=False`` is the
    only failure path so the route layer can render the ``message`` as
    an inline hint without ever raising HTTP 500.
    """
    result = _empty_result()
    if not parsed["valid"]:
        result["message"] = parsed["error"] or "invalid input"
        log.info("palette_commands.rejected", reason=result["message"])
        return result

    name = parsed["command"]
    args = parsed["args"]
    log.info("palette_commands.exec", command=name, argc=len(args))

    if name == "help":
        result["ok"] = True
        result["message"] = "see :data"
        result["data"] = {"catalogue": list(CATALOGUE)}
        return result

    if name == "pin":
        return await _exec_pin(args, pin=True)
    if name == "unpin":
        return await _exec_pin(args, pin=False)
    if name == "tag":
        return await _exec_tag(args)
    if name == "note":
        return await _exec_note(args)
    if name == "goto":
        return _exec_goto(args)
    if name == "pause":
        return await _exec_kv("capture_screens_disabled", "1", "capture paused")
    if name == "resume":
        return await _exec_kv("capture_screens_disabled", "0", "capture resumed")
    if name == "mic":
        return await _exec_mic(args)
    if name == "theme":
        return await _exec_theme(args)

    # Catalogue / dispatcher drift — should be unreachable because the
    # parser already validated against ``_NAMES``.
    result["message"] = f"no handler wired for :{name}"
    log.warning("palette_commands.no_handler", command=name)
    return result


async def _exec_pin(args: list[str], *, pin: bool) -> ExecutionResult:
    result = _empty_result()
    verb = "pin" if pin else "unpin"
    if len(args) != 1:
        result["message"] = f":{verb} requires exactly one screenshot id"
        return result
    sid = _parse_int(args[0])
    if sid is None or sid <= 0:
        result["message"] = f"invalid screenshot id: {args[0]!r}"
        return result
    async with get_connection() as conn:
        shot = await get_screenshot(conn, sid)
        if shot is None:
            result["message"] = f"screenshot {sid} not found"
            return result
        if pin:
            await pin_screenshot(conn, sid)
            new_tier = "pinned"
        else:
            await unpin_screenshot(conn, sid)
            new_tier = "hot"
    result["ok"] = True
    result["message"] = f"screenshot {sid} → {new_tier}"
    result["data"] = {"screenshot_id": sid, "tier": new_tier}
    return result


async def _exec_tag(args: list[str]) -> ExecutionResult:
    result = _empty_result()
    if len(args) < 2:
        result["message"] = ":tag requires <id> and <tag>"
        return result
    sid = _parse_int(args[0])
    if sid is None or sid <= 0:
        result["message"] = f"invalid screenshot id: {args[0]!r}"
        return result
    tag = " ".join(args[1:]).strip().lower()
    if not tag:
        result["message"] = "tag name is empty"
        return result
    async with get_connection() as conn:
        shot = await get_screenshot(conn, sid)
        if shot is None:
            result["message"] = f"screenshot {sid} not found"
            return result
        tag_id = await create_tag(conn, name=tag)
        await tag_screenshot(conn, sid, tag_id)
    result["ok"] = True
    result["message"] = f"screenshot {sid} tagged #{tag}"
    result["data"] = {"screenshot_id": sid, "tag": tag, "tag_id": tag_id}
    return result


async def _exec_note(args: list[str]) -> ExecutionResult:
    result = _empty_result()
    if not args:
        result["message"] = ":note requires text"
        return result
    body = args[0]
    if not body.strip():
        result["message"] = ":note requires non-empty text"
        return result
    async with get_connection() as conn:
        note_id = await insert_inbox_note(conn, body=body, source="palette")
    result["ok"] = True
    result["message"] = f"note #{note_id} saved"
    result["data"] = {"note_id": note_id}
    return result


def _exec_goto(args: list[str]) -> ExecutionResult:
    result = _empty_result()
    if len(args) != 1:
        result["message"] = ":goto requires an ISO-8601 timestamp"
        return result
    iso = args[0].strip()
    if not iso:
        result["message"] = ":goto requires an ISO-8601 timestamp"
        return result
    # We don't try to parse the timestamp here — the existing /goto
    # handler does its own parsing and returns 400 on garbage input.
    # Pre-validating would force us to keep two parsers in sync.
    result["ok"] = True
    result["redirect"] = f"/goto?at={iso}"
    result["message"] = f"redirect → /goto?at={iso}"
    result["data"] = {"at": iso}
    return result


async def _exec_kv(key: str, value: str, message: str) -> ExecutionResult:
    """Shared helper for :pause / :resume (and anything else that's a
    plain kv toggle). Uses :func:`set_kv` so the SQL is parametrised."""
    result = _empty_result()
    async with get_connection() as conn:
        await set_kv(conn, key, value)
    result["ok"] = True
    result["message"] = message
    result["data"] = {"kv_key": key, "kv_value": value}
    return result


async def _exec_mic(args: list[str]) -> ExecutionResult:
    result = _empty_result()
    if len(args) != 1 or args[0].lower() not in _MIC_VALID:
        result["message"] = ":mic requires on|off"
        return result
    # The kv key stores the *paused* state — ``:mic off`` means the
    # user wants the mic off, i.e. paused=1.
    flip = args[0].lower()
    paused = "1" if flip == "off" else "0"
    async with get_connection() as conn:
        await set_kv(conn, "audio_capture_paused_live", paused)
    result["ok"] = True
    result["message"] = f"mic {flip}"
    result["data"] = {"audio_capture_paused_live": paused}
    return result


async def _exec_theme(args: list[str]) -> ExecutionResult:
    result = _empty_result()
    if len(args) != 1 or args[0].lower() not in _THEME_VALID:
        result["message"] = ":theme requires dark|light|auto"
        return result
    theme = args[0].lower()
    async with get_connection() as conn:
        # Read-before-write keeps the structlog line useful for
        # operators tailing the audit feed.
        previous = await get_kv(conn, "theme") or "dark"
        await set_kv(conn, "theme", theme)
    result["ok"] = True
    result["message"] = f"theme {previous} → {theme}"
    result["data"] = {"theme": theme, "previous": previous}
    return result
