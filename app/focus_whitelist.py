"""Focus-session app whitelist — inverse of focus_blocklist (v1.47).

While a :mod:`app.focus` session is running, the capture loop should
skip any shot whose ``active_window.app_name`` is *not* on this list.
Outside an active session the list has no effect, mirroring the
conditional-block contract of :mod:`app.focus_blocklist`.

Semantics of ``is_focus_allowed``:

    * empty whitelist → "open mode", always returns ``True``. The
      operator has not opted into an allowlist; capture proceeds as if
      this feature did not exist (the blocklist still applies upstream).
    * non-empty list  → returns ``True`` only when the normalised app
      name is present. The hot path is one indexed-PK probe per capture
      iteration.

Normalisation matches :mod:`app.focus_blocklist`: ``str.strip().casefold()``
on both read and write so the operator can type ``"VS Code"``, ``" vs code "``
or ``"VSCODE"`` and the Win32-reported ``"Code"`` resolves consistently —
the *raw* user input is stored, but compared in normalised form.

The async helpers all open their own connection via
:func:`app.storage.db.get_connection`. Failure modes never raise on the
hot path: :func:`is_focus_allowed` swallows DB errors and returns
``True`` (open mode) so a broken whitelist cannot silently nuke
capture. The write helpers do surface ``ValueError`` for empty input —
the settings form is the only caller and we want bad submissions to
land as 400, not as a no-op.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.focus_whitelist")


class WhitelistEntry(TypedDict):
    """One ``focus_whitelist`` row, normalised for templates and JSON."""

    id: int
    app_name: str
    added_at: str


def _normalise(app_name: str) -> str:
    """Collapse ``app_name`` to the canonical comparison form.

    Strips surrounding whitespace and casefolds. Both writes and reads
    go through this helper so a user typing ``"Slack "`` and the Win32
    API reporting ``"slack"`` resolve to the same row.
    """
    return app_name.strip().casefold()


async def add_app(app_name: str) -> int:
    """Insert ``app_name`` into the whitelist. Returns the row id.

    Raises :class:`ValueError` when the normalised string is empty —
    the settings form is the only caller, and an empty submission is
    a UI bug we want to surface as a 400 rather than silently swallow.
    When the app is already present, returns the existing row id
    (``INSERT OR IGNORE`` followed by a lookup) so the caller can treat
    the operation as idempotent.
    """
    normalised = _normalise(app_name)
    if not normalised:
        msg = "app_name is required"
        raise ValueError(msg)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO focus_whitelist (app_name) VALUES (?)",
            (normalised,),
        )
        await conn.commit()
        new_id = int(cursor.lastrowid or 0)
        if new_id == 0:
            # Row already existed; fetch its id so the caller still
            # gets a usable handle (idempotent contract).
            lookup = await conn.execute(
                "SELECT id FROM focus_whitelist WHERE app_name = ? LIMIT 1",
                (normalised,),
            )
            row = await lookup.fetchone()
            if row is None:
                # Vanishingly unlikely race — the row was deleted between
                # the INSERT OR IGNORE and the lookup. Surface a clear
                # error rather than returning a misleading ``0``.
                msg = "focus_whitelist row vanished mid-insert"
                raise RuntimeError(msg)
            new_id = int(row["id"])
    log.info("focus_whitelist.added", app_name=normalised, row_id=new_id)
    return new_id


async def remove_app(app_name: str) -> None:
    """Remove ``app_name`` from the whitelist. Idempotent.

    The argument is normalised before deletion so the caller may pass
    either the raw Win32 form or whatever shape the admin UI rendered.
    Missing rows are silently fine — the contract is "after this
    returns, ``app_name`` is not in the whitelist".
    """
    normalised = _normalise(app_name)
    if not normalised:
        return
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM focus_whitelist WHERE app_name = ?",
            (normalised,),
        )
        await conn.commit()
    log.info("focus_whitelist.removed", app_name=normalised)


async def remove_by_id(row_id: int) -> None:
    """Delete a whitelist row by primary key. Idempotent.

    The admin UI prefers id-based deletion over name-based deletion so
    the form action survives a rename, and so the URL never has to
    URL-encode whatever odd characters an app name carries.
    """
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM focus_whitelist WHERE id = ?",
            (row_id,),
        )
        await conn.commit()
    log.info("focus_whitelist.removed_by_id", row_id=row_id)


async def list_apps() -> list[WhitelistEntry]:
    """Return every whitelist row, alphabetically sorted by ``app_name``.

    The shape is a list of :class:`WhitelistEntry` dicts so the route
    layer can render the admin table without re-shaping ``aiosqlite.Row``
    objects (which are not JSON-serialisable as-is).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, app_name, added_at FROM focus_whitelist ORDER BY app_name"
        )
        rows = await cursor.fetchall()
    return [
        WhitelistEntry(
            id=int(row["id"]),
            app_name=str(row["app_name"]),
            added_at=str(row["added_at"]),
        )
        for row in rows
    ]


async def is_focus_allowed(app_name: str | None) -> bool:
    """Return ``True`` when ``app_name`` is allowed by the whitelist.

    Contract:

    * Empty whitelist → always ``True`` (open mode; the operator has
      not opted into an allowlist).
    * Non-empty list  → ``True`` iff the normalised name is present.

    ``None`` and empty / whitespace-only ``app_name`` arguments
    short-circuit to the open-mode behaviour: we never want a probe
    that could not identify the foreground app to suppress a capture.
    Failure modes (DB locked, transient I/O) downgrade to ``True`` and
    log at DEBUG — a broken whitelist must never silently halt the
    capture loop.

    Called once per capture iteration from
    :mod:`app.workers.capture_loop`; the SQLite probe is indexed on
    ``app_name`` (UNIQUE constraint) and cheap.
    """
    try:
        async with get_connection() as conn:
            count_cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM focus_whitelist"
            )
            count_row = await count_cursor.fetchone()
            total = int(count_row["n"]) if count_row is not None else 0
            if total == 0:
                return True
            if app_name is None:
                return True
            normalised = _normalise(app_name)
            if not normalised:
                return True
            probe = await conn.execute(
                "SELECT 1 FROM focus_whitelist WHERE app_name = ? LIMIT 1",
                (normalised,),
            )
            hit = await probe.fetchone()
    except Exception as exc:
        log.debug("focus_whitelist.is_allowed_failed", error=str(exc))
        return True
    return hit is not None


async def record_skip(app_name: str | None, session_id: int) -> None:
    """Log a single whitelist-driven skip event.

    No new table — the audit signal is the structlog line at INFO. The
    capture loop's structured log stream is the canonical place a
    reviewer goes to ask "why was that minute blank?", and the v1.40
    privacy-mode sentinel already proved that one INFO line per skip is
    enough to debug the feature in production.
    """
    log.info(
        "focus_whitelist.skipped",
        app_name=app_name,
        session_id=session_id,
    )


__all__ = [
    "WhitelistEntry",
    "add_app",
    "is_focus_allowed",
    "list_apps",
    "record_skip",
    "remove_app",
    "remove_by_id",
]
