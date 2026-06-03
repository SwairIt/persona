"""App-name display aliases — rename ``devenv.exe`` to ``Visual Studio``.

Every screenshot row carries an ``app_name`` (Win32 executable / window
class — ``devenv.exe``, ``Code.exe``, ``chrome.exe``). Those raw strings
are great identifiers but ugly labels; this module is the overlay that
maps them to a human-friendly display name without touching the
underlying ``screenshots.app_name`` column (which the capture loop, the
search index and the dedup pipeline all read directly).

The public surface has two halves:

* **Async storage helpers** — :func:`set_alias` / :func:`get_alias` /
  :func:`list_all` / :func:`delete_alias` — used by the admin UI route
  and by tests. They go through :func:`app.storage.db.get_connection` so
  they participate in the same WAL/foreign-key pragma setup the rest of
  the app uses.

* **Sync :func:`resolve`** — invoked by Jinja's ``app_alias`` filter,
  which runs inside the synchronous template render path and cannot
  ``await``. It opens its own short-lived ``sqlite3`` connection against
  the same DB file; SQLite's WAL mode lets that reader coexist with the
  async writers. The lookup result is cached in-process for the
  process's lifetime so a timeline with 200 cells doesn't pay 200
  round-trips — and the cache is invalidated explicitly by every write
  path in this module.

Lookup semantics: an empty / unknown ``original_name`` resolves to
itself, never raises. Callers can therefore pipe any string through the
filter without an ``if`` guard — the worst case is the same string back.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger("persona.app_aliases")

# Process-wide cache for the synchronous Jinja path. A single-element
# list holds the cache slot so we can mutate it without a ``global``
# statement — ruff's PLW0603 dislikes module-level rebinding, and the
# list-as-cell pattern is the standard workaround. ``None`` means
# "needs reload"; an empty dict means "loaded, no aliases configured".
# The lock keeps two concurrent renders from each hitting SQLite when
# the cache is cold — only the first one pays.
_cache_slot: list[dict[str, str] | None] = [None]
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Async helpers (admin UI + tests)
# ---------------------------------------------------------------------------


async def set_alias(original: str, display: str) -> None:
    """Upsert ``display`` as the alias for ``original``.

    Both inputs are stripped; ``original`` is required (raises
    :class:`ValueError`), an empty ``display`` is treated as "no alias"
    and routed through :func:`delete_alias` so the row never lingers as
    a no-op overlay.
    """
    original = original.strip()
    display = display.strip()
    if not original:
        msg = "original_name is required"
        raise ValueError(msg)
    if not display:
        await delete_alias(original)
        return
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO app_alias (original_name, display_name)
            VALUES (?, ?)
            ON CONFLICT(original_name) DO UPDATE SET
                display_name = excluded.display_name
            """,
            (original, display),
        )
        await conn.commit()
    _invalidate_cache()
    log.info("app_aliases.set", original=original, display=display)


async def get_alias(original: str) -> str | None:
    """Return the stored display name for ``original`` or ``None``."""
    key = original.strip()
    if not key:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT display_name FROM app_alias WHERE original_name = ?",
            (key,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["display_name"])


async def list_all() -> list[dict[str, str]]:
    """Return every stored alias, ordered by ``original_name`` ASC.

    Each item exposes ``original_name`` and ``display_name`` — the admin
    UI renders one row per item so the operator can see which raw
    strings already have an override.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT original_name, display_name FROM app_alias "
            "ORDER BY original_name ASC"
        )
        rows = await cursor.fetchall()
    return [
        {
            "original_name": str(row["original_name"]),
            "display_name": str(row["display_name"]),
        }
        for row in rows
    ]


async def delete_alias(original: str) -> None:
    """Drop the alias for ``original``. Idempotent — missing rows are fine."""
    key = original.strip()
    if not key:
        return
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM app_alias WHERE original_name = ?",
            (key,),
        )
        await conn.commit()
    _invalidate_cache()
    log.info("app_aliases.deleted", original=key)


# ---------------------------------------------------------------------------
# Sync resolver (Jinja filter)
# ---------------------------------------------------------------------------


def resolve(name: Any) -> str:
    """Return the display name for ``name`` or ``name`` itself if no alias.

    Synchronous on purpose: Jinja filters cannot ``await``. Uses a
    short-lived stdlib ``sqlite3`` connection (WAL mode → safe alongside
    the async writers) and an in-process cache to amortise the lookup
    across every cell of a single render.

    Never raises. Non-string / empty / unconfigured-DB inputs all return
    the input coerced to ``str`` — a template must not 500 on a missing
    alias table.
    """
    if name is None:
        return ""
    key = str(name).strip()
    if not key:
        return str(name)
    table = _load_cache()
    return table.get(key, str(name))


# ---------------------------------------------------------------------------
# Cache internals
# ---------------------------------------------------------------------------


def _load_cache() -> dict[str, str]:
    """Return the cached alias table, populating it on first read."""
    cached = _cache_slot[0]
    if cached is not None:
        return cached
    with _cache_lock:
        # Re-check under the lock — another caller may have populated it
        # between our first read and the ``acquire``.
        existing = _cache_slot[0]
        if existing is not None:
            return existing
        loaded = _read_aliases_sync(get_settings().db_path)
        _cache_slot[0] = loaded
        return loaded


def _invalidate_cache() -> None:
    """Drop the in-process cache after a write. Called by every mutator."""
    with _cache_lock:
        _cache_slot[0] = None


def _read_aliases_sync(db_path: Path) -> dict[str, str]:
    """Synchronously load every alias row into a dict. Returns ``{}`` on error.

    Any failure (missing DB, missing table because migrations haven't
    run yet, corrupt row) collapses to an empty mapping so the
    :func:`resolve` filter degrades gracefully to identity. A render
    must never 500 because of an alias lookup.
    """
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT original_name, display_name FROM app_alias"
            )
            rows = cursor.fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]): str(row[1]) for row in rows}


__all__ = [
    "delete_alias",
    "get_alias",
    "list_all",
    "resolve",
    "set_alias",
]
