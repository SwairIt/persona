"""Dashboard grid editor service layer (v1.37).

Persona's /dashboard has accumulated a fixed sequence of hand-coded
widgets over many versions (today_vs_average since v1.35, voice_note
since v1.36, capture_status / latest_digest / top_apps_7d / streak_card
/ budget_meter even earlier). The render order + visibility was
hard-coded in the template, so users who wanted to demote a
rarely-used widget had to wait for someone to ship a flag.

This module exposes the storage layer behind the v1.37 grid editor.
The companion route module
:mod:`app.web.routes.dashboard_widget_editor` is the HTML and JSON
surface; the /dashboard renderer itself can adopt
:func:`list_active_widgets` in a later tick — this module is
deliberately importable in isolation so the dashboard route never has
to learn about persistence to keep working today.

Two distinct concepts coexist here:

* **Catalogue** (:data:`WIDGET_CATALOGUE`): the *canonical* list of
  every widget the codebase knows how to render. Code-defined, never
  read from the database — adding a widget is a code change, not a row
  insert. The catalogue carries human-readable metadata (title,
  description) for the editor UI plus a ``default_position`` used to
  bootstrap an empty install.
* **Slots** (:func:`list_slots`): the user's *saved* layout, persisted
  in ``dashboard_grid_slot``. A slot points at a catalogue key, holds
  the user's chosen position + enabled flag, and survives
  catalogue-side renames via the orphan-drop in
  :func:`upsert_widgets`.

The catalogue includes a couple of "future" widgets
(``pinned_shots_strip``, ``entity_cloud``, ``ocr_confidence_mini``)
that aren't seeded by the migration but appear in the editor's
Available column so the renderer-side wiring can land independently of
the editor — when a future PR adds those, the user can just enable
them without a follow-up migration.
"""

from __future__ import annotations

import json
from typing import Any, Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.dashboard_widgets")


class WidgetMeta(TypedDict):
    """Catalogue entry shape — title/description/default_position."""

    title: str
    description: str
    default_position: int


# The single source of truth for what a "widget" *can* be. The
# editor's Available column is built from
# ``WIDGET_CATALOGUE.keys() - currently_active_keys`` so adding an
# entry here is the only hand-edit needed to expose a new widget in
# the picker (the renderer-side wiring is independent).
#
# ``default_position`` mirrors the seed order in migration
# ``112_dashboard_widgets.sql`` for the seven shipping widgets and
# slots the three "future" widgets at 7, 8, 9 — so if the renderer
# starts honouring them before the user reorders, the resulting layout
# stays close to the historical hard-coded order.
WIDGET_CATALOGUE: Final[dict[str, WidgetMeta]] = {
    "today_vs_average": {
        "title": "Today vs average",
        "description": "Activity ratio vs the trailing 7-day mean.",
        "default_position": 0,
    },
    "capture_status": {
        "title": "Capture status",
        "description": "Is the screen / mic capture pipeline running.",
        "default_position": 1,
    },
    "latest_digest": {
        "title": "Latest weekly digest",
        "description": "Most recent weekly_card summary card.",
        "default_position": 2,
    },
    "voice_note": {
        "title": "Voice note",
        "description": "One-click recorder for a quick spoken note.",
        "default_position": 3,
    },
    "top_apps_7d": {
        "title": "Top apps (7d)",
        "description": "Bar chart of the most-used apps over the last week.",
        "default_position": 4,
    },
    "streak_card": {
        "title": "Streak card",
        "description": "Consecutive active days for capture + notes.",
        "default_position": 5,
    },
    "budget_meter": {
        "title": "Daily budget meter",
        "description": "Storage / LLM-spend gauge against the daily cap.",
        "default_position": 6,
    },
    # ── future widgets (not seeded, picker only) ─────────────────────
    "pinned_shots_strip": {
        "title": "Pinned shots strip",
        "description": "Horizontal strip of recently pinned screenshots.",
        "default_position": 7,
    },
    "entity_cloud": {
        "title": "Entity cloud",
        "description": "Tag-cloud of recurring people / projects / topics.",
        "default_position": 8,
    },
    "ocr_confidence_mini": {
        "title": "OCR confidence (mini)",
        "description": "Average OCR confidence over the last 24 hours.",
        "default_position": 9,
    },
}


class WidgetUpdate(TypedDict, total=False):
    """One row of the bulk-update payload accepted by :func:`upsert_widgets`.

    ``slot_id`` is omitted for newly-added widgets (the picker side of
    the editor) — the upsert path inserts a fresh row in that case.
    ``options_json`` is optional; absent means "leave any existing
    JSON blob untouched".
    """

    slot_id: int
    widget_key: str
    position: int
    enabled: bool
    options_json: str | None


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Marshal an ``aiosqlite.Row`` into a plain dict + decode JSON."""
    options_raw: str | None = row["options_json"]
    options: dict[str, Any] | None
    if options_raw is None or options_raw == "":
        options = None
    else:
        try:
            decoded = json.loads(options_raw)
        except json.JSONDecodeError:
            # A corrupt options blob must not break the whole layout
            # render; surface as null and log so the operator can
            # investigate. The next save from the editor overwrites
            # the bad JSON with a clean value.
            log.warning(
                "dashboard_widgets.options_json.decode_failed",
                slot_id=int(row["slot_id"]),
                widget_key=str(row["widget_key"]),
            )
            options = None
        else:
            options = decoded if isinstance(decoded, dict) else None

    return {
        "slot_id": int(row["slot_id"]),
        "widget_key": str(row["widget_key"]),
        "position": int(row["position"]),
        "enabled": bool(row["enabled"]),
        "options": options,
        "updated_at": str(row["updated_at"]),
    }


async def list_slots() -> list[dict[str, Any]]:
    """Return *every* slot (enabled + disabled) ordered by position.

    Used by the editor — the Available column is computed as
    ``catalogue_keys - {row['widget_key'] for row in list_slots()}`` so
    a disabled widget still counts as "claimed" and doesn't appear
    twice (once in Active-toggled-off, once in Available).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slot_id, widget_key, position, enabled, options_json, updated_at "
            "FROM dashboard_grid_slot "
            "ORDER BY position ASC, slot_id ASC",
        )
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def list_active_widgets() -> list[dict[str, Any]]:
    """Return only enabled slots, ordered by position.

    This is the contract the future /dashboard renderer will call —
    keeping it separate from :func:`list_slots` means the renderer
    never has to filter or skip rows, and the editor's "Active column"
    intentionally shows disabled-but-saved widgets too (different
    callers, different needs).

    Unknown catalogue keys are dropped so a widget removed from
    :data:`WIDGET_CATALOGUE` after the row was persisted can't break
    the render — the slot stays in the DB until the next editor save
    naturally cleans it up.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slot_id, widget_key, position, enabled, options_json, updated_at "
            "FROM dashboard_grid_slot "
            "WHERE enabled = 1 "
            "ORDER BY position ASC, slot_id ASC",
        )
        rows = await cursor.fetchall()

    active: list[dict[str, Any]] = []
    for row in rows:
        marshaled = _row_to_dict(row)
        key = marshaled["widget_key"]
        if key not in WIDGET_CATALOGUE:
            log.info(
                "dashboard_widgets.unknown_key_skipped",
                widget_key=key,
                slot_id=marshaled["slot_id"],
            )
            continue
        meta = WIDGET_CATALOGUE[key]
        marshaled["title"] = meta["title"]
        marshaled["description"] = meta["description"]
        active.append(marshaled)

    log.info("dashboard_widgets.list_active", count=len(active))
    return active


def _validate_update(raw: dict[str, Any]) -> WidgetUpdate:
    """Normalise + bounds-check one row of the bulk-update payload.

    Raises ``ValueError`` for bad input — the caller (the route layer)
    turns that into a 400. Keeping validation in the service layer
    means future callers (e.g. a CLI importer) get the same checks for
    free.
    """
    widget_key = raw.get("widget_key")
    if not isinstance(widget_key, str) or widget_key not in WIDGET_CATALOGUE:
        msg = f"unknown widget_key: {widget_key!r}"
        raise ValueError(msg)

    position = raw.get("position", 0)
    if not isinstance(position, int) or position < 0:
        msg = f"position must be a non-negative int, got {position!r}"
        raise ValueError(msg)

    enabled_raw = raw.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        msg = f"enabled must be bool, got {enabled_raw!r}"
        raise ValueError(msg)

    update: WidgetUpdate = {
        "widget_key": widget_key,
        "position": position,
        "enabled": enabled_raw,
    }

    slot_id = raw.get("slot_id")
    if slot_id is not None:
        if not isinstance(slot_id, int) or slot_id <= 0:
            msg = f"slot_id must be a positive int when present, got {slot_id!r}"
            raise ValueError(msg)
        update["slot_id"] = slot_id

    options_json = raw.get("options_json")
    if options_json is not None:
        if not isinstance(options_json, str):
            msg = f"options_json must be str|null, got {options_json!r}"
            raise ValueError(msg)
        # Round-trip parse so a malformed blob fails at write time
        # rather than corrupting the row and surfacing later as a
        # silent ``None`` in :func:`_row_to_dict`.
        try:
            json.loads(options_json)
        except json.JSONDecodeError as exc:
            msg = f"options_json is not valid JSON: {exc}"
            raise ValueError(msg) from exc
        update["options_json"] = options_json

    return update


async def upsert_widgets(updates: list[dict[str, Any]]) -> int:
    """Replace the entire layout from the editor's bulk submit.

    The editor POSTs the complete desired list — Active column rows
    first (enabled=True, position by array order), Available rows
    explicitly omitted (treated as disabled). We:

    1. Validate every row up-front so a single bad payload entry
       rolls back the whole transaction instead of leaving a
       half-applied layout.
    2. UPDATE rows that arrive with a ``slot_id`` so existing slot
       identity is preserved across reorders.
    3. INSERT rows without a ``slot_id`` (newly-added widgets from
       the picker).
    4. DELETE any pre-existing slot whose ``slot_id`` is not in the
       payload — this is how the editor models "user removed widget
       X from the picker entirely". Disabled-but-kept widgets must
       still be POSTed with ``enabled=False`` to survive.

    Returns the count of rows now visible in the table.
    """
    validated: list[WidgetUpdate] = [_validate_update(raw) for raw in updates]

    async with get_connection() as conn:
        await conn.execute("BEGIN")
        try:
            cursor = await conn.execute(
                "SELECT slot_id FROM dashboard_grid_slot",
            )
            existing_rows = await cursor.fetchall()
            existing_ids: set[int] = {int(row["slot_id"]) for row in existing_rows}
            kept_ids: set[int] = set()

            for entry in validated:
                slot_id = entry.get("slot_id")
                if slot_id is not None and slot_id in existing_ids:
                    # UPDATE existing slot. We only touch options_json
                    # when the caller provided it — passing ``None``
                    # would clear the user's saved blob, which the
                    # editor never wants to do implicitly.
                    if "options_json" in entry:
                        await conn.execute(
                            "UPDATE dashboard_grid_slot SET "
                            "widget_key = ?, position = ?, enabled = ?, "
                            "options_json = ?, updated_at = datetime('now') "
                            "WHERE slot_id = ?",
                            (
                                entry["widget_key"],
                                entry["position"],
                                1 if entry["enabled"] else 0,
                                entry["options_json"],
                                slot_id,
                            ),
                        )
                    else:
                        await conn.execute(
                            "UPDATE dashboard_grid_slot SET "
                            "widget_key = ?, position = ?, enabled = ?, "
                            "updated_at = datetime('now') "
                            "WHERE slot_id = ?",
                            (
                                entry["widget_key"],
                                entry["position"],
                                1 if entry["enabled"] else 0,
                                slot_id,
                            ),
                        )
                    kept_ids.add(slot_id)
                else:
                    # INSERT a new slot (either no slot_id provided or
                    # the provided id no longer exists — both collapse
                    # to "create me").
                    await conn.execute(
                        "INSERT INTO dashboard_grid_slot "
                        "(widget_key, position, enabled, options_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            entry["widget_key"],
                            entry["position"],
                            1 if entry["enabled"] else 0,
                            entry.get("options_json"),
                        ),
                    )

            to_delete = existing_ids - kept_ids
            for stale_id in to_delete:
                await conn.execute(
                    "DELETE FROM dashboard_grid_slot WHERE slot_id = ?",
                    (stale_id,),
                )

            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

        cursor = await conn.execute(
            "SELECT COUNT(*) AS total FROM dashboard_grid_slot",
        )
        row = await cursor.fetchone()
        total = int(row["total"]) if row is not None else 0

    log.info(
        "dashboard_widgets.upsert",
        applied=len(validated),
        deleted=len(existing_ids - kept_ids),
        total=total,
    )
    return total
