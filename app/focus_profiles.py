"""Per-app focus profiles — one-click bundles of capture-loop knobs (v1.49).

A *focus profile* is a saved combination of five settings the operator
toggles together when switching contexts:

* ``capture_interval_seconds_live`` — capture cadence
* ``capture_screens_disabled``      — master screen kill switch
* ``audio_capture_paused_live``     — master mic kill switch
* ``meeting_pause_enabled``         — v1.19 smart-pause toggle
* ``theme``                         — dark / light / auto

Switching profiles is a single HTTP POST that flips every relevant kv row
in one transaction, so the capture loop, audio worker, meeting detector
and theme renderer all see the new state on their next tick without any
extra wiring.

Four presets ship out of the box (Deep Work / Pair Coding / Meeting /
Reading) and can be re-installed at any time — :func:`install_preset`
uses ``INSERT OR IGNORE`` so a re-run is a no-op for existing rows.
"""

from __future__ import annotations

from typing import TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import set_kv

log = get_logger("persona.focus_profiles")


class FocusProfile(TypedDict):
    """One ``focus_profile`` row, normalised for templates and JSON."""

    id: int
    name: str
    description: str | None
    capture_interval_seconds: float | None
    audio_paused: bool
    blocklist_apps: str | None
    meeting_pause_enabled: bool
    theme: str | None
    is_active: bool
    created_at: str


class _PresetSpec(TypedDict):
    """Shape of a single entry in :data:`PRESET_PROFILES`."""

    name: str
    description: str
    capture_interval_seconds: float | None
    audio_paused: bool
    blocklist_apps: str | None
    meeting_pause_enabled: bool
    theme: str | None


# Sensible-default bundles. Each preset mirrors the schema column shape
# exactly so :func:`install_preset` can splat the dict straight into the
# ``INSERT`` statement. ``capture_interval_seconds`` is REAL because the
# kv row stores it as a stringified float; ``None`` means "do not touch
# the kv row when activating".
PRESET_PROFILES: list[_PresetSpec] = [
    {
        "name": "Deep Work",
        "description": (
            "Slow 30-second cadence, audio paused, dark theme. "
            "Mic + smart-pause off — pure screen telemetry while you "
            "concentrate."
        ),
        "capture_interval_seconds": 30.0,
        "audio_paused": True,
        "blocklist_apps": None,
        "meeting_pause_enabled": False,
        "theme": "dark",
    },
    {
        "name": "Pair Coding",
        "description": (
            "Fast 5-second cadence so two people poking at one IDE stay "
            "on the same page. Audio on; smart-pause on."
        ),
        "capture_interval_seconds": 5.0,
        "audio_paused": False,
        "blocklist_apps": None,
        "meeting_pause_enabled": True,
        "theme": None,
    },
    {
        "name": "Meeting",
        "description": (
            "60-second cadence + audio paused so neither the grabber "
            "nor the mic interferes with the call; dark theme to keep "
            "glare off your face on a webcam."
        ),
        "capture_interval_seconds": 60.0,
        "audio_paused": True,
        "blocklist_apps": None,
        "meeting_pause_enabled": True,
        "theme": "dark",
    },
    {
        "name": "Reading",
        "description": (
            "60-second cadence, everything paused. You're reading a "
            "PDF and want zero ambient telemetry while you think."
        ),
        "capture_interval_seconds": 60.0,
        "audio_paused": True,
        "blocklist_apps": None,
        "meeting_pause_enabled": False,
        "theme": None,
    },
]


def _row_to_profile(row: aiosqlite.Row) -> FocusProfile:
    """Normalise an ``aiosqlite.Row`` from ``focus_profile`` into a typed dict.

    Centralised so both :func:`list_profiles` and :func:`activate_profile`
    hand back identical shapes — the route layer and the JSON endpoint
    treat the dict as ground truth, not the raw row.
    """
    raw_interval = row["capture_interval_seconds"]
    return FocusProfile(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]) if row["description"] is not None else None,
        capture_interval_seconds=float(raw_interval) if raw_interval is not None else None,
        audio_paused=int(row["audio_paused"]) == 1,
        blocklist_apps=str(row["blocklist_apps"]) if row["blocklist_apps"] is not None else None,
        meeting_pause_enabled=int(row["meeting_pause_enabled"]) == 1,
        theme=str(row["theme"]) if row["theme"] is not None else None,
        is_active=int(row["is_active"]) == 1,
        created_at=str(row["created_at"]),
    )


async def install_preset(name: str) -> int:
    """Insert the preset named ``name`` from :data:`PRESET_PROFILES`.

    Returns the row id (newly inserted or already present). Idempotent —
    a second call on the same name is a no-op because of the UNIQUE
    constraint on ``focus_profile.name``. Raises :class:`ValueError`
    when ``name`` does not match a known preset so a typo'd POST surfaces
    as a 400 rather than a silent miss.
    """
    spec: _PresetSpec | None = next(
        (preset for preset in PRESET_PROFILES if preset["name"] == name),
        None,
    )
    if spec is None:
        msg = f"unknown preset profile: {name!r}"
        raise ValueError(msg)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO focus_profile "
            "(name, description, capture_interval_seconds, audio_paused, "
            " blocklist_apps, meeting_pause_enabled, theme) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                spec["name"],
                spec["description"],
                spec["capture_interval_seconds"],
                1 if spec["audio_paused"] else 0,
                spec["blocklist_apps"],
                1 if spec["meeting_pause_enabled"] else 0,
                spec["theme"],
            ),
        )
        await conn.commit()
        new_id = int(cursor.lastrowid or 0)
        if new_id == 0:
            lookup = await conn.execute(
                "SELECT id FROM focus_profile WHERE name = ? LIMIT 1",
                (spec["name"],),
            )
            row = await lookup.fetchone()
            if row is None:
                msg = "focus_profile preset row vanished mid-insert"
                raise RuntimeError(msg)
            new_id = int(row["id"])
    log.info("focus_profiles.preset_installed", name=spec["name"], row_id=new_id)
    return new_id


async def list_profiles() -> list[FocusProfile]:
    """Return every focus profile, newest first.

    The shape is a list of :class:`FocusProfile` dicts so the route layer
    can render the page without re-shaping ``aiosqlite.Row`` objects
    (which are not JSON-serialisable as-is).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, description, capture_interval_seconds, "
            "       audio_paused, blocklist_apps, meeting_pause_enabled, "
            "       theme, is_active, created_at "
            "FROM focus_profile "
            "ORDER BY created_at DESC, id DESC"
        )
        rows = await cursor.fetchall()
    return [_row_to_profile(row) for row in rows]


async def activate_profile(profile_id: int) -> FocusProfile:
    """Mark ``profile_id`` as the single active profile and apply its kv rows.

    The transaction zeros out every other profile's ``is_active`` flag
    first, then sets this one to 1 — so the partial index over
    ``is_active = 1`` always points at exactly one row. After the DB
    commit we splat the profile's settings into the same ``kv_settings``
    rows the capture loop, audio worker, meeting detector and theme
    renderer already read on every tick: ``capture_interval_seconds_live``,
    ``capture_screens_disabled``, ``audio_capture_paused_live``,
    ``meeting_pause_enabled`` and ``theme``.

    ``capture_screens_disabled`` is derived from ``capture_interval_seconds``:
    a profile that explicitly sets the interval is "screens on", a profile
    that does not (``None``) leaves the row untouched. We never *enable*
    the screen kill switch from a profile activation — that would surprise
    the operator the first time they swap profiles and find their timeline
    blank — but we make sure to *clear* it whenever a profile that does
    want screens active is selected.

    NULL fields on the profile mean "do not touch the corresponding kv
    row", so an operator can build hybrid profiles that only flip one
    knob.

    Raises :class:`LookupError` when ``profile_id`` does not exist so
    the POST handler can surface a 404.
    """
    async with get_connection() as conn:
        probe = await conn.execute(
            "SELECT id, name, description, capture_interval_seconds, "
            "       audio_paused, blocklist_apps, meeting_pause_enabled, "
            "       theme, is_active, created_at "
            "FROM focus_profile WHERE id = ? LIMIT 1",
            (profile_id,),
        )
        row = await probe.fetchone()
        if row is None:
            msg = f"focus_profile {profile_id} does not exist"
            raise LookupError(msg)
        await conn.execute("UPDATE focus_profile SET is_active = 0 WHERE is_active = 1")
        await conn.execute(
            "UPDATE focus_profile SET is_active = 1 WHERE id = ?",
            (profile_id,),
        )

        profile = _row_to_profile(row)

        # Apply kv side-effects inside the same transaction so a crash
        # mid-commit doesn't leave the kv rows ahead of the focus_profile
        # table — the next page load would render the wrong "active" chip.
        if profile["capture_interval_seconds"] is not None:
            await set_kv(
                conn,
                "capture_interval_seconds_live",
                str(profile["capture_interval_seconds"]),
            )
            # A profile that sets an interval is implicitly "screens on" —
            # clear the master kill switch so the operator never has to
            # remember it exists. We never *set* it to "1" from here.
            await set_kv(conn, "capture_screens_disabled", "0")
        await set_kv(
            conn,
            "audio_capture_paused_live",
            "1" if profile["audio_paused"] else "0",
        )
        await set_kv(
            conn,
            "meeting_pause_enabled",
            "1" if profile["meeting_pause_enabled"] else "0",
        )
        if profile["theme"] is not None:
            await set_kv(conn, "theme", profile["theme"])
        await conn.commit()

    activated = FocusProfile(
        id=profile["id"],
        name=profile["name"],
        description=profile["description"],
        capture_interval_seconds=profile["capture_interval_seconds"],
        audio_paused=profile["audio_paused"],
        blocklist_apps=profile["blocklist_apps"],
        meeting_pause_enabled=profile["meeting_pause_enabled"],
        theme=profile["theme"],
        is_active=True,
        created_at=profile["created_at"],
    )
    log.info(
        "focus_profiles.activated",
        profile_id=profile_id,
        name=activated["name"],
        capture_interval_seconds=activated["capture_interval_seconds"],
        audio_paused=activated["audio_paused"],
        meeting_pause_enabled=activated["meeting_pause_enabled"],
        theme=activated["theme"],
    )
    return activated


async def create_profile(
    name: str,
    description: str | None = None,
    capture_interval_seconds: float | None = None,
    *,
    audio_paused: bool = False,
    blocklist_apps: str | None = None,
    meeting_pause_enabled: bool = True,
    theme: str | None = None,
) -> int:
    """Insert a custom focus profile and return its row id.

    Raises :class:`ValueError` when ``name`` is empty or already taken —
    the settings form is the only caller and we want bad submissions to
    land as 400, not as a silent no-op or an IntegrityError leaking
    through to the operator.
    """
    cleaned_name = name.strip()
    if not cleaned_name:
        msg = "name is required"
        raise ValueError(msg)
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO focus_profile "
                "(name, description, capture_interval_seconds, audio_paused, "
                " blocklist_apps, meeting_pause_enabled, theme) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cleaned_name,
                    description.strip() if description else None,
                    capture_interval_seconds,
                    1 if audio_paused else 0,
                    blocklist_apps.strip() if blocklist_apps else None,
                    1 if meeting_pause_enabled else 0,
                    theme,
                ),
            )
            await conn.commit()
    except aiosqlite.IntegrityError as exc:
        msg = f"focus profile {cleaned_name!r} already exists"
        raise ValueError(msg) from exc
    new_id = int(cursor.lastrowid or 0)
    log.info("focus_profiles.created", name=cleaned_name, row_id=new_id)
    return new_id


async def delete_profile(profile_id: int) -> None:
    """Drop the given focus profile. Idempotent.

    Deleting the active profile is allowed — the kv rows it had applied
    stay where they are. The operator can either activate another
    profile to overwrite them or edit the underlying settings pages
    directly.
    """
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM focus_profile WHERE id = ?",
            (profile_id,),
        )
        await conn.commit()
    log.info("focus_profiles.deleted", profile_id=profile_id)


__all__ = [
    "PRESET_PROFILES",
    "FocusProfile",
    "activate_profile",
    "create_profile",
    "delete_profile",
    "install_preset",
    "list_profiles",
]
