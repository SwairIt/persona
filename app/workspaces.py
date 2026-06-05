"""Workspace contexts — holistic one-click setting bundles (v1.64).

A *workspace* is the next generalisation of v1.49's :mod:`app.focus_profiles`:
instead of bundling just the capture-loop knobs, it pulls in five
subsystems and switches them as one unit:

* ``theme``                         — light / dark / auto kv row
* ``capture_interval_seconds_live`` — capture-loop cadence kv row
* ``focus_profile_id``              — chained activation through the
                                      existing v1.49 focus-profile helper
* ``blocklist_apps_json``           — JSON array of app names; reserved
                                      for the v1.32 capture-blocklist UI
                                      to read lazily
* ``default_timeline_filter``       — opaque text the active-window
                                      timeline view can use as its
                                      default ``?filter=`` query string

Three presets ship out of the box (Coder, Writer, Reader). The
:func:`install_preset` helper is idempotent — a re-install on the same
name is a no-op via ``INSERT OR IGNORE``.
"""

from __future__ import annotations

import json
from typing import TypedDict

import aiosqlite

from app.focus_profiles import activate_profile as activate_focus_profile
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import set_kv

log = get_logger("persona.workspaces")


class Workspace(TypedDict):
    """One ``workspace`` row, normalised for templates and JSON."""

    id: int
    name: str
    description: str | None
    theme: str | None
    capture_interval_seconds: float | None
    focus_profile_id: int | None
    blocklist_apps: list[str]
    default_timeline_filter: str | None
    is_active: bool
    created_at: str


class _PresetSpec(TypedDict):
    """Shape of a single entry in :data:`PRESET_WORKSPACES`."""

    name: str
    description: str
    theme: str | None
    capture_interval_seconds: float | None
    focus_profile_name: str | None
    blocklist_apps: list[str]
    default_timeline_filter: str | None


# Sensible-default bundles. Each preset matches the columns the helper
# layer writes — ``focus_profile_name`` is resolved to a real FK at
# install time so a missing focus profile degrades gracefully (the
# workspace simply has ``focus_profile_id`` NULL and stops chaining).
PRESET_WORKSPACES: list[_PresetSpec] = [
    {
        "name": "Coder",
        "description": (
            "Dark theme, 5-second cadence for tight timeline coverage of "
            "IDE work, chained to the Pair Coding focus profile, "
            "messengers blocked, timeline filtered to coding apps."
        ),
        "theme": "dark",
        "capture_interval_seconds": 5.0,
        "focus_profile_name": "Pair Coding",
        "blocklist_apps": ["Slack", "Telegram", "Discord"],
        "default_timeline_filter": "category=coding",
    },
    {
        "name": "Writer",
        "description": (
            "Light theme, slow 30-second cadence so the timeline does not "
            "explode while drafting prose, chained to the Deep Work focus "
            "profile, browsers blocked, timeline filtered to writing apps."
        ),
        "theme": "light",
        "capture_interval_seconds": 30.0,
        "focus_profile_name": "Deep Work",
        "blocklist_apps": ["Chrome", "Firefox", "Edge"],
        "default_timeline_filter": "category=writing",
    },
    {
        "name": "Reader",
        "description": (
            "Auto theme, slowest 60-second cadence, chained to the Reading "
            "focus profile so audio + smart-pause are off, everything "
            "social blocked, timeline filtered to readers."
        ),
        "theme": "auto",
        "capture_interval_seconds": 60.0,
        "focus_profile_name": "Reading",
        "blocklist_apps": ["Slack", "Telegram", "Discord", "Twitter"],
        "default_timeline_filter": "category=reading",
    },
]


def _parse_blocklist_json(raw: str | None) -> list[str]:
    """Decode the ``blocklist_apps_json`` column into a list of app names.

    Returns ``[]`` for NULL / empty / malformed JSON so the route layer
    never has to defend against a half-broken row. We deliberately keep
    parsing best-effort here — a malformed blob is logged at WARN and
    the caller carries on with an empty list rather than 500-ing the
    page.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("workspaces.blocklist_json_malformed", raw=raw[:120])
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item.strip()]


def _row_to_workspace(row: aiosqlite.Row) -> Workspace:
    """Normalise an ``aiosqlite.Row`` from ``workspace`` into a typed dict.

    Centralised so :func:`list_workspaces`, :func:`activate_workspace`
    and the JSON endpoint hand back identical shapes.
    """
    raw_interval = row["capture_interval_seconds"]
    raw_focus = row["focus_profile_id"]
    return Workspace(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]) if row["description"] is not None else None,
        theme=str(row["theme"]) if row["theme"] is not None else None,
        capture_interval_seconds=float(raw_interval) if raw_interval is not None else None,
        focus_profile_id=int(raw_focus) if raw_focus is not None else None,
        blocklist_apps=_parse_blocklist_json(
            str(row["blocklist_apps_json"]) if row["blocklist_apps_json"] is not None else None,
        ),
        default_timeline_filter=(
            str(row["default_timeline_filter"])
            if row["default_timeline_filter"] is not None
            else None
        ),
        is_active=int(row["is_active"]) == 1,
        created_at=str(row["created_at"]),
    )


async def _lookup_focus_profile_id(
    conn: aiosqlite.Connection,
    profile_name: str | None,
) -> int | None:
    """Resolve a focus-profile name to its row id, or ``None`` if absent.

    Used by :func:`install_preset` so the preset row keeps a real FK to
    an already-installed focus profile. When the preset references a
    profile the operator has not installed yet, we silently store NULL
    so the workspace still installs and simply does not chain the focus
    activation later.
    """
    if not profile_name:
        return None
    cursor = await conn.execute(
        "SELECT id FROM focus_profile WHERE name = ? LIMIT 1",
        (profile_name,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return int(row["id"])


async def install_preset(name: str) -> int:
    """Insert the preset named ``name`` from :data:`PRESET_WORKSPACES`.

    Returns the row id (newly inserted or already present). Idempotent
    via ``INSERT OR IGNORE`` on the UNIQUE name column. Raises
    :class:`ValueError` when ``name`` does not match a known preset so
    a typo'd POST surfaces as a 400 rather than a silent miss.
    """
    spec: _PresetSpec | None = next(
        (preset for preset in PRESET_WORKSPACES if preset["name"] == name),
        None,
    )
    if spec is None:
        msg = f"unknown preset workspace: {name!r}"
        raise ValueError(msg)
    async with get_connection() as conn:
        focus_id = await _lookup_focus_profile_id(conn, spec["focus_profile_name"])
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO workspace "
            "(name, description, theme, capture_interval_seconds, "
            " focus_profile_id, blocklist_apps_json, default_timeline_filter) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                spec["name"],
                spec["description"],
                spec["theme"],
                spec["capture_interval_seconds"],
                focus_id,
                json.dumps(spec["blocklist_apps"]),
                spec["default_timeline_filter"],
            ),
        )
        await conn.commit()
        new_id = int(cursor.lastrowid or 0)
        if new_id == 0:
            lookup = await conn.execute(
                "SELECT id FROM workspace WHERE name = ? LIMIT 1",
                (spec["name"],),
            )
            row = await lookup.fetchone()
            if row is None:
                msg = "workspace preset row vanished mid-insert"
                raise RuntimeError(msg)
            new_id = int(row["id"])
    log.info("workspaces.preset_installed", name=spec["name"], row_id=new_id)
    return new_id


async def list_workspaces() -> list[Workspace]:
    """Return every workspace, newest first.

    The shape is a list of :class:`Workspace` dicts so the route layer
    and the JSON endpoint share one normalised view.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, description, theme, capture_interval_seconds, "
            "       focus_profile_id, blocklist_apps_json, "
            "       default_timeline_filter, is_active, created_at "
            "FROM workspace "
            "ORDER BY created_at DESC, id DESC"
        )
        rows = await cursor.fetchall()
    return [_row_to_workspace(row) for row in rows]


async def activate_workspace(ws_id: int) -> Workspace:
    """Mark ``ws_id`` active and fan out its bundle to every subsystem.

    Activation runs in two phases:

    1. **DB phase, single transaction** — zero every other workspace's
       ``is_active`` flag, set this one to 1, and splat ``theme`` +
       ``capture_interval_seconds_live`` into ``kv_settings`` so the
       theme renderer and capture loop see the new values on their
       next tick.
    2. **Chain phase, separate call** — if the workspace has a
       ``focus_profile_id``, invoke :func:`app.focus_profiles.activate_profile`
       so the focus-profile bundle layers on top. The focus helper
       opens its own transaction; we explicitly do not nest because
       sqlite does not support nested transactions and the focus
       activation is independently idempotent.

    NULL columns mean "do not touch the matching kv row" so the
    operator can build hybrid workspaces that only flip a subset of
    knobs. Raises :class:`LookupError` when ``ws_id`` does not exist
    so the POST handler can surface a 404.
    """
    async with get_connection() as conn:
        probe = await conn.execute(
            "SELECT id, name, description, theme, capture_interval_seconds, "
            "       focus_profile_id, blocklist_apps_json, "
            "       default_timeline_filter, is_active, created_at "
            "FROM workspace WHERE id = ? LIMIT 1",
            (ws_id,),
        )
        row = await probe.fetchone()
        if row is None:
            msg = f"workspace {ws_id} does not exist"
            raise LookupError(msg)
        await conn.execute("UPDATE workspace SET is_active = 0 WHERE is_active = 1")
        await conn.execute(
            "UPDATE workspace SET is_active = 1 WHERE id = ?",
            (ws_id,),
        )

        workspace = _row_to_workspace(row)

        # Apply kv side-effects inside the same transaction so a crash
        # mid-commit does not leave the kv rows ahead of the workspace
        # table — the next page load would render the wrong "active"
        # chip.
        if workspace["theme"] is not None:
            await set_kv(conn, "theme", workspace["theme"])
        if workspace["capture_interval_seconds"] is not None:
            await set_kv(
                conn,
                "capture_interval_seconds_live",
                str(workspace["capture_interval_seconds"]),
            )
        await conn.commit()

    # Chain through to focus-profile activation outside our transaction
    # because the focus helper opens its own. We swallow LookupError so
    # a workspace pointing at a deleted profile still activates cleanly
    # (the FK is ON DELETE SET NULL, but a race during deletion could
    # still leave a dangling id mid-activation).
    if workspace["focus_profile_id"] is not None:
        try:
            await activate_focus_profile(workspace["focus_profile_id"])
        except LookupError:
            log.warning(
                "workspaces.focus_profile_missing",
                workspace_id=ws_id,
                focus_profile_id=workspace["focus_profile_id"],
            )

    activated = Workspace(
        id=workspace["id"],
        name=workspace["name"],
        description=workspace["description"],
        theme=workspace["theme"],
        capture_interval_seconds=workspace["capture_interval_seconds"],
        focus_profile_id=workspace["focus_profile_id"],
        blocklist_apps=workspace["blocklist_apps"],
        default_timeline_filter=workspace["default_timeline_filter"],
        is_active=True,
        created_at=workspace["created_at"],
    )
    log.info(
        "workspaces.activated",
        workspace_id=ws_id,
        name=activated["name"],
        theme=activated["theme"],
        capture_interval_seconds=activated["capture_interval_seconds"],
        focus_profile_id=activated["focus_profile_id"],
        blocklist_count=len(activated["blocklist_apps"]),
        default_timeline_filter=activated["default_timeline_filter"],
    )
    return activated


async def create_workspace(
    name: str,
    description: str | None = None,
    theme: str | None = None,
    capture_interval_seconds: float | None = None,
    *,
    focus_profile_id: int | None = None,
    blocklist_apps: list[str] | None = None,
    default_timeline_filter: str | None = None,
) -> int:
    """Insert a custom workspace and return its row id.

    Raises :class:`ValueError` when ``name`` is empty or already taken
    so the settings form surfaces bad submissions as 400 instead of a
    silent no-op or an ``IntegrityError`` leaking through.
    """
    cleaned_name = name.strip()
    if not cleaned_name:
        msg = "name is required"
        raise ValueError(msg)
    blocklist = blocklist_apps or []
    blocklist_json = json.dumps([item.strip() for item in blocklist if item.strip()])
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO workspace "
                "(name, description, theme, capture_interval_seconds, "
                " focus_profile_id, blocklist_apps_json, default_timeline_filter) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cleaned_name,
                    description.strip() if description else None,
                    theme,
                    capture_interval_seconds,
                    focus_profile_id,
                    blocklist_json,
                    default_timeline_filter.strip() if default_timeline_filter else None,
                ),
            )
            await conn.commit()
    except aiosqlite.IntegrityError as exc:
        msg = f"workspace {cleaned_name!r} already exists"
        raise ValueError(msg) from exc
    new_id = int(cursor.lastrowid or 0)
    log.info("workspaces.created", name=cleaned_name, row_id=new_id)
    return new_id


async def delete_workspace(ws_id: int) -> None:
    """Drop the given workspace. Idempotent.

    Deleting the active workspace is allowed — the kv rows and focus
    profile it had applied stay where they are. The operator can either
    activate another workspace to overwrite them or edit the underlying
    settings pages directly.
    """
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM workspace WHERE id = ?",
            (ws_id,),
        )
        await conn.commit()
    log.info("workspaces.deleted", workspace_id=ws_id)


__all__ = [
    "PRESET_WORKSPACES",
    "Workspace",
    "activate_workspace",
    "create_workspace",
    "delete_workspace",
    "install_preset",
    "list_workspaces",
]
