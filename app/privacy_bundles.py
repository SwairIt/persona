"""Privacy-bundle preset library (v1.43).

Background — v1.42 shipped privacy mode as a single hard-coded
``PRIVACY_PATTERNS`` tuple inside :mod:`app.privacy_mode`. That made
the catalogue auditable but rigid: a user who wanted to shield a
dating app or a crypto wallet had to fork the source. v1.43 keeps the
hard-coded tuple as a *fallback* and adds a user-editable
``privacy_bundle`` table on top: grouped, named pattern lists that
the operator can install from preset cards or grow by hand.

Two-table layout (see ``migrations/117_privacy_bundles.sql``):

* ``privacy_bundle`` — one row per bundle (name UNIQUE, enabled flag).
* ``privacy_bundle_pattern`` — child rows; one pattern each, ``ON
  DELETE CASCADE`` so dropping a bundle drops its patterns.

The hot path is :mod:`app.privacy_mode`'s compile cache, which calls
:func:`list_active_patterns` once per capture iteration; that read is
cheap (single indexed join) and the SHA-256 fingerprint short-circuits
recompilation when nothing changed. See the module docstring of
:mod:`app.capture_blocklist` for the same pattern in the regex
blocklist.

This module deliberately ships *only* the install + query API. CRUD
(toggle / delete / add pattern) lives in
:mod:`app.web.routes.privacy_bundles_admin` and operates on the
tables directly — the admin layer is thin enough that a separate
storage shim would just shuffle SQL between files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.logging_setup import get_logger
from app.privacy_mode import PRIVACY_PATTERNS, invalidate_active_patterns_cache
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.privacy_bundles")


# Preset bundle catalogue. Each preset is a literal dict the admin UI
# renders as an "Install" card; clicking ``install_preset(name)`` runs
# the INSERT chain. The patterns lean on the same substring-match
# semantics as :mod:`app.privacy_mode` (re.IGNORECASE, ``.search()``):
# operators add literal substrings, not anchored regexes, so the cards
# stay readable at a glance.
#
# Curation policy: each preset shields a *category* of sensitive
# surface, not a vendor catalogue. We add the leaders in each category
# plus the Russian-speaking-market ones that the hard-coded tuple
# already covers; an operator who needs a niche app simply edits the
# bundle after installing.
PRESET_BUNDLES: list[dict[str, object]] = [
    {
        "name": "incognito_browsing",
        "description": (
            "Private-browsing windows across Chrome, Firefox, Edge, "
            "Safari, Brave, Opera. Substring match — covers the "
            "browser's own 'Incognito'/'Private Browsing' affix in the "
            "window title."
        ),
        "patterns": [
            "Incognito",
            "InPrivate",
            "Private Browsing",
            "Private Window",
            "Приватное окно",
            "Приватный просмотр",
        ],
    },
    {
        "name": "password_managers",
        "description": (
            "Password-manager surfaces. Matches the app-name field as "
            "well as window titles like '1Password — vault'."
        ),
        "patterns": [
            "KeePass",
            "1Password",
            "Bitwarden",
            "LastPass",
            "Dashlane",
            "NordPass",
            "Proton Pass",
            "Enpass",
            "RoboForm",
            "Authy",
        ],
    },
    {
        "name": "banking_apps",
        "description": (
            "Online-banking and personal-finance interfaces. Matches "
            "both the Russian-speaking-market giants and the global "
            "leaders by substring."
        ),
        "patterns": [
            "Bank of",
            "Сбербанк",
            "Тинькофф",
            "Tinkoff",
            "ВТБ",
            "Альфа-Банк",
            "Райффайзен",
            "banking",
            "Online Banking",
            "Chase",
            "Wells Fargo",
            "Revolut",
            "Wise",
            "PayPal",
        ],
    },
    {
        "name": "crypto_wallets",
        "description": (
            "Cryptocurrency wallet and exchange surfaces. Seed "
            "phrases, private keys and balances are exactly the "
            "shoulder-surf material screenshots leak by default."
        ),
        "patterns": [
            "MetaMask",
            "Phantom",
            "Trust Wallet",
            "Ledger Live",
            "Trezor",
            "Exodus",
            "Coinbase",
            "Binance",
            "Kraken",
            "Bybit",
            "OKX",
            "Uniswap",
        ],
    },
    {
        "name": "dating_apps",
        "description": (
            "Dating-app surfaces. Private by social convention even "
            "before the data itself — captures here are the classic "
            "'over-the-shoulder embarrassment' case."
        ),
        "patterns": [
            "Tinder",
            "Bumble",
            "Hinge",
            "OkCupid",
            "Badoo",
            "Mamba",
            "Grindr",
            "Match.com",
            "Pure",
            "Feeld",
        ],
    },
    {
        "name": "mental_health_apps",
        "description": (
            "Therapy, mood-journal and crisis-support apps. Even the "
            "app *name* in a capture log is itself sensitive — privacy "
            "mode keeps only a hashed audit row."
        ),
        "patterns": [
            "BetterHelp",
            "Talkspace",
            "Calm",
            "Headspace",
            "Wysa",
            "Woebot",
            "Moodfit",
            "Daylio",
            "Sanvello",
            "MindShift",
            "Yasno",
            "Алимент",
        ],
    },
]


async def install_preset(preset_name: str) -> dict[str, object]:
    """Install one preset bundle by name. Idempotent.

    Looks the preset up in :data:`PRESET_BUNDLES`, INSERTs the parent
    ``privacy_bundle`` row with ``ON CONFLICT(name) DO NOTHING`` so a
    repeat install is a no-op, and then INSERTs every pattern. When
    the parent row was a duplicate we return early — re-INSERTing the
    patterns would double them, and the operator's intent ("install
    this preset") is already satisfied.

    Returns ``{"bundle_id": int, "inserted": bool, "skipped_duplicate":
    bool}`` so the caller can render a "installed N patterns" /
    "already installed" toast.
    """
    preset = _find_preset(preset_name)
    if preset is None:
        log.warning("privacy_bundles.preset_not_found", name=preset_name)
        return {
            "bundle_id": 0,
            "inserted": False,
            "skipped_duplicate": False,
        }

    name = str(preset["name"])
    description = str(preset["description"])
    patterns = list(preset["patterns"]) if isinstance(preset["patterns"], list) else []

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO privacy_bundle (name, description) "
            "VALUES (?, ?) "
            "ON CONFLICT(name) DO NOTHING",
            (name, description),
        )
        # ``cursor.lastrowid`` is 0 on a conflict no-op; resolve to the
        # *existing* bundle id so the caller always has a target row.
        if cursor.rowcount == 0:
            existing = await conn.execute(
                "SELECT id FROM privacy_bundle WHERE name = ?",
                (name,),
            )
            row = await existing.fetchone()
            bundle_id = int(row["id"]) if row is not None else 0
            await conn.commit()
            log.info(
                "privacy_bundles.install_skipped_duplicate",
                name=name,
                bundle_id=bundle_id,
            )
            return {
                "bundle_id": bundle_id,
                "inserted": False,
                "skipped_duplicate": True,
            }

        bundle_id = int(cursor.lastrowid or 0)
        for pattern in patterns:
            pattern_str = str(pattern).strip()
            if not pattern_str:
                continue
            await conn.execute(
                "INSERT INTO privacy_bundle_pattern (bundle_id, pattern) "
                "VALUES (?, ?)",
                (bundle_id, pattern_str),
            )
        await conn.commit()
        log.info(
            "privacy_bundles.installed",
            name=name,
            bundle_id=bundle_id,
            pattern_count=len(patterns),
        )

    # Cache invalidation lives at the privacy_mode layer; do it inline
    # here so a programmatic install_preset() (CLI, tests, future
    # cron seeding) does not silently leave the compile cache stale.
    # Routes additionally invalidate after their own writes — both
    # paths are cheap (single global = None).
    invalidate_active_patterns_cache()
    return {
        "bundle_id": bundle_id,
        "inserted": True,
        "skipped_duplicate": False,
    }


async def list_active_patterns() -> list[str]:
    """Return every active pattern, DB-first then hard-coded fallback.

    Joins ``privacy_bundle`` (enabled=1) with
    ``privacy_bundle_pattern`` and flattens the result. Then appends
    the hard-coded :data:`app.privacy_mode.PRIVACY_PATTERNS` tuple so
    a fresh database (no installed bundles) still gets the v1.42
    behaviour — operators can rely on the safety floor without having
    to remember to click "Install" right after setup.

    Failure mode: a DB error returns the hard-coded tuple alone, with
    a WARNING in the log. Privacy mode must never silently degrade to
    *less* protective than v1.42.
    """
    db_patterns: list[str] = []
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT p.pattern AS pattern "
                "FROM privacy_bundle b "
                "JOIN privacy_bundle_pattern p ON p.bundle_id = b.id "
                "WHERE b.enabled = 1"
            )
            rows = await cursor.fetchall()
            db_patterns = [
                str(row["pattern"]) for row in rows if row["pattern"] is not None
            ]
    except Exception as exc:
        log.warning("privacy_bundles.list_failed", error=str(exc))
        db_patterns = []

    # Deduplicate while preserving insertion order so the admin UI
    # surfaces "your patterns first, then the safety floor".
    merged: list[str] = []
    seen: set[str] = set()
    for pat in (*db_patterns, *PRIVACY_PATTERNS):
        if pat not in seen:
            seen.add(pat)
            merged.append(pat)
    return merged


async def list_bundles(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Return every bundle (enabled + disabled) with its pattern count.

    Joined view used by the admin UI: ``id, name, description,
    enabled, created_at, pattern_count``. ``LEFT JOIN`` so a freshly
    created empty bundle still shows up with count=0.
    """
    cursor = await conn.execute(
        "SELECT b.id AS id, b.name AS name, b.description AS description, "
        "       b.enabled AS enabled, b.created_at AS created_at, "
        "       COUNT(p.id) AS pattern_count "
        "FROM privacy_bundle b "
        "LEFT JOIN privacy_bundle_pattern p ON p.bundle_id = b.id "
        "GROUP BY b.id, b.name, b.description, b.enabled, b.created_at "
        "ORDER BY b.id DESC"
    )
    return list(await cursor.fetchall())


async def list_patterns_for_bundle(
    conn: aiosqlite.Connection,
    bundle_id: int,
) -> list[aiosqlite.Row]:
    """Return every pattern row for ``bundle_id`` in insertion order."""
    cursor = await conn.execute(
        "SELECT id, pattern FROM privacy_bundle_pattern "
        "WHERE bundle_id = ? ORDER BY id",
        (bundle_id,),
    )
    return list(await cursor.fetchall())


async def create_bundle(
    conn: aiosqlite.Connection,
    *,
    name: str,
    description: str | None,
) -> int:
    """Create an empty bundle. Raises ``ValueError`` on bad input.

    The UNIQUE constraint on ``name`` does the de-dup work at the
    storage layer — we surface the ``IntegrityError`` as a friendlier
    ``ValueError`` so the route can return a 400.
    """
    name_clean = name.strip()
    if not name_clean:
        msg = "name is required"
        raise ValueError(msg)
    description_clean = (description or "").strip() or None
    try:
        cursor = await conn.execute(
            "INSERT INTO privacy_bundle (name, description) VALUES (?, ?)",
            (name_clean, description_clean),
        )
    except Exception as exc:
        msg = f"could not create bundle: {exc}"
        raise ValueError(msg) from exc
    await conn.commit()
    new_id = int(cursor.lastrowid or 0)
    log.info(
        "privacy_bundles.bundle_created",
        bundle_id=new_id,
        name=name_clean,
    )
    return new_id


async def add_pattern_to_bundle(
    conn: aiosqlite.Connection,
    *,
    bundle_id: int,
    pattern: str,
) -> int:
    """Append a pattern to an existing bundle. Returns the new row id."""
    pattern_clean = pattern.strip()
    if not pattern_clean:
        msg = "pattern is required"
        raise ValueError(msg)
    cursor = await conn.execute(
        "INSERT INTO privacy_bundle_pattern (bundle_id, pattern) "
        "VALUES (?, ?)",
        (bundle_id, pattern_clean),
    )
    await conn.commit()
    new_id = int(cursor.lastrowid or 0)
    log.info(
        "privacy_bundles.pattern_added",
        bundle_id=bundle_id,
        pattern_id=new_id,
        pattern=pattern_clean,
    )
    return new_id


async def toggle_bundle(conn: aiosqlite.Connection, bundle_id: int) -> None:
    """Flip the ``enabled`` flag on ``bundle_id``. No-op when missing."""
    await conn.execute(
        "UPDATE privacy_bundle SET enabled = 1 - enabled WHERE id = ?",
        (bundle_id,),
    )
    await conn.commit()
    log.info("privacy_bundles.bundle_toggled", bundle_id=bundle_id)


async def delete_bundle(conn: aiosqlite.Connection, bundle_id: int) -> None:
    """Delete a bundle (and its patterns via ON DELETE CASCADE)."""
    await conn.execute(
        "DELETE FROM privacy_bundle WHERE id = ?",
        (bundle_id,),
    )
    await conn.commit()
    log.info("privacy_bundles.bundle_deleted", bundle_id=bundle_id)


def _find_preset(preset_name: str) -> dict[str, object] | None:
    """Return the preset entry by name, or ``None`` if absent."""
    for preset in PRESET_BUNDLES:
        if preset["name"] == preset_name:
            return preset
    return None


__all__ = [
    "PRESET_BUNDLES",
    "add_pattern_to_bundle",
    "create_bundle",
    "delete_bundle",
    "install_preset",
    "list_active_patterns",
    "list_bundles",
    "list_patterns_for_bundle",
    "toggle_bundle",
]
