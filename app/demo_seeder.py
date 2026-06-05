"""Interactive demo-data seeder (v1.47).

Populates a fresh Persona install with plausible-looking rows so the
timeline, hourly cards, daily-pin, and notes pages have something to
render for screencasts and README screenshots. Every row inserted by
:func:`seed_demo_data` is tagged ``screenshots.is_demo = 1`` (see
migration ``129_demo_data.sql``) so :func:`purge_demo_data` can wipe
just the seeded rows without touching real capture data.

The seeder is *intentionally* offline-only: it never reaches the OCR
worker, never asks the capture loop to grab a frame, never writes a
thumbnail file, and never enqueues an embedding job. It manipulates
only the four storage tables that drive the empty-state placeholders
we want to fill — ``screenshots``, ``screenshot_notes``, ``hourly_card``,
and ``daily_pin`` — using parametrised SQL exclusively.

Safety
------
Before inserting anything, :func:`seed_demo_data` counts *real*
(``is_demo = 0``) rows in ``screenshots``. If that count exceeds
:data:`_MAX_REAL_ROWS_FOR_SAFETY` (50, deliberately conservative) the
function refuses to run and raises :class:`SeederRefused`. This is the
guard that prevents an operator from accidentally polluting a
months-old database with fake data when they meant to wipe and reseed
a fresh dev install.

Determinism
-----------
The generator uses :class:`random.Random` seeded from
:data:`_RANDOM_SEED` so two runs with the same arguments produce the
same row contents. That is convenient for tests and for predictable
screenshots, but the timestamps are still *relative* to wall-clock
``now`` so the timeline always looks "today / yesterday / this week"
no matter when the seed is run.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Iterable

    import aiosqlite

log = get_logger("persona.demo_seeder")

# ---------------------------------------------------------------------------
# Safety + tuning knobs
# ---------------------------------------------------------------------------

#: Refuse to seed if the screenshots table already has more real
#: (``is_demo = 0``) rows than this. 50 is well below the ~hour-of-capture
#: floor on a real install (default cadence ≈ 5s → ~720 rows/hour) so a
#: genuinely-used database always trips this guard.
_MAX_REAL_ROWS_FOR_SAFETY: Final[int] = 50

#: Deterministic RNG seed. Lets the gallery look identical across reruns
#: which is desirable for screenshots posted to a README.
_RANDOM_SEED: Final[int] = 0xDEDA

#: Hard upper bounds — generous, but stop unbounded `days * shots_per_day`
#: explosions from a typo'd POST body. The defaults (7 * 30 = 210) sit
#: comfortably below both.
_MAX_DAYS: Final[int] = 60
_MAX_SHOTS_PER_DAY: Final[int] = 200

# ---------------------------------------------------------------------------
# Plausible content corpora
# ---------------------------------------------------------------------------

_APPS: Final[tuple[str, ...]] = (
    "VSCode",
    "Chrome",
    "Slack",
    "Figma",
    "Linear",
    "Notion",
    "Zoom",
    "Terminal",
    "Mail",
    "Spotify",
)

# Per-app window-title fragments. ``{x}`` is substituted from a
# corresponding corpus below so titles read like real working sessions
# rather than the same string repeated 200 times.
_TITLE_TEMPLATES: Final[dict[str, tuple[str, ...]]] = {
    "VSCode": (
        "{file} — persona",
        "{file} — schoolproject",
        "{file} (working tree) — Visual Studio Code",
    ),
    "Chrome": (
        "{tab} — Google Chrome",
        "{tab} - GitHub",
        "{tab} | Stack Overflow",
    ),
    "Slack": (
        "#{channel} | Persona workspace",
        "DM with {person} | Slack",
        "Threads | Slack",
    ),
    "Figma": (
        "{file} - Figma",
        "{file} / Components - Figma",
    ),
    "Linear": (
        "PER-{ticket} {tab} - Linear",
        "My issues - Linear",
    ),
    "Notion": (
        "{tab} - Notion",
        "Daily journal - {tab}",
    ),
    "Zoom": (
        "Zoom Meeting - {person}",
        "Zoom - {tab}",
    ),
    "Terminal": (
        "{person}@dev - {file}",
        "pwsh - git status",
        "bash - pytest -q",
    ),
    "Mail": (
        "Inbox - {person}",
        "Re: {tab}",
    ),
    "Spotify": (
        "{tab} - Spotify",
        "Focus playlist - Spotify",
    ),
}

_FILES: Final[tuple[str, ...]] = (
    "main.py",
    "demo_seeder.py",
    "schema.sql",
    "rate_advisor.py",
    "hourly_card.py",
    "daily_pin.py",
    "test_capture.py",
    "settings.py",
    "README.md",
    "pyproject.toml",
)

_TABS: Final[tuple[str, ...]] = (
    "Persona dashboard",
    "Migration plan",
    "SQLite WAL tuning",
    "Hourly card design",
    "Demo seeder review",
    "FastAPI lifespan",
    "Alpine.js docs",
    "Tailwind config",
    "Pull request #142",
    "Notes archive",
)

_CHANNELS: Final[tuple[str, ...]] = (
    "general",
    "persona-dev",
    "design",
    "incidents",
    "random",
)

_PEOPLE: Final[tuple[str, ...]] = (
    "Anna",
    "Boris",
    "Clara",
    "Dmitry",
    "Elena",
    "Fyodor",
)

_OCR_TEMPLATES: Final[tuple[str, ...]] = (
    "TODO: revisit {topic} next week",
    "PR #{ticket} ready for review — {topic}",
    "{topic} draft v{ticket}",
    "Meeting notes: {topic} {ticket}",
    "{topic}: looks good, ship it",
    "{topic} - blocked on {ticket}",
)

_TOPICS: Final[tuple[str, ...]] = (
    "demo seeder",
    "hourly cards",
    "rate advisor",
    "daily pin",
    "OCR rerun",
    "capture loop",
    "tier decay",
    "notes export",
)

_NOTE_BODIES: Final[tuple[str, ...]] = (
    "Decision: keep partial index on is_demo until v1.50.",
    "Follow-up: cross-link this shot to the migration PR.",
    "Idea: surface demo-data banner on /timeline when seeded.",
    "Reminder: ask user if seeder should also touch capture_events.",
    "Why this matters: empty timeline kills first-run impression.",
    "Don't forget the dedup_groups stub — phash collisions matter.",
    "Hourly cards need at least one row or /memory 404s on empty DB.",
    "Note: seeder is deterministic via fixed RNG seed for screenshots.",
)

# Lower-case, comma-separated keywords pasted verbatim into hourly_card
# rows — what the heuristic OCR-top-words extractor would emit.
_TOP_WORDS_PER_HOUR: Final[tuple[str, ...]] = (
    "persona,seeder,demo,sqlite,index",
    "rate,advisor,capture,interval,dedup",
    "hourly,card,memory,llm,enrichment",
    "notes,export,markdown,obsidian,sync",
    "ocr,rerun,history,annotation,revision",
)

_PIN_TEMPLATES: Final[tuple[str, ...]] = (
    "Shipped demo seeder + migration 129. Reviewed PRs from {person}.",
    "Deep work on hourly cards. Long meeting with {person}, then patched OCR rerun.",
    "Quiet day — refactored capture loop, drafted notes for {person}.",
)

# Default working window for the synthetic timestamps. 9:00 → 18:00
# inclusive of the start, exclusive of the end, weekdays only — matches
# the heuristic the rest of Persona uses for "active hours".
_WORK_HOUR_START: Final[int] = 9
_WORK_HOUR_END: Final[int] = 18

# Standard "monitor" geometry so the synthetic rows look like a real
# laptop capture. Both width and height are NOT NULL in the schema.
_DEFAULT_WIDTH: Final[int] = 1920
_DEFAULT_HEIGHT: Final[int] = 1080


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SeederRefused(RuntimeError):
    """Raised when the seeder refuses to run because real data is present.

    The route surfaces this as a 409 so the operator sees an explicit
    "cowardly refusing" message instead of silently appending fake rows
    to a populated install.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    """Format ``value`` as the ISO-8601 string Persona stores in TEXT cols."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _spread_timestamps(
    rng: random.Random,
    days: int,
    shots_per_day: int,
    *,
    now: datetime,
) -> list[datetime]:
    """Return ``shots_per_day * weekdays`` timestamps inside 09:00→18:00.

    Skips Saturday (weekday 5) and Sunday (weekday 6) — the working-hours
    band reads as "developer with a calendar" rather than "robot at
    3 a.m.". Within each weekday we draw uniform random seconds inside
    the 9-hour window then sort ascending so the timeline rendering is
    stable.
    """
    timestamps: list[datetime] = []
    today = now.date()
    window_seconds = (_WORK_HOUR_END - _WORK_HOUR_START) * 3600
    # ``days`` includes today: we walk back ``days - 1`` calendar days.
    for offset in range(days):
        day = today - timedelta(days=offset)
        weekday = day.weekday()
        if weekday >= 5:  # Saturday + Sunday — skip.
            continue
        for _ in range(shots_per_day):
            sec_offset = rng.randrange(window_seconds)
            ts = datetime(
                day.year,
                day.month,
                day.day,
                _WORK_HOUR_START,
                0,
                0,
                tzinfo=UTC,
            ) + timedelta(seconds=sec_offset)
            timestamps.append(ts)
    timestamps.sort()
    return timestamps


def _make_window_title(rng: random.Random, app_name: str) -> str:
    """Pick a plausible window-title for ``app_name`` from the templates."""
    template = rng.choice(_TITLE_TEMPLATES[app_name])
    return template.format(
        file=rng.choice(_FILES),
        tab=rng.choice(_TABS),
        channel=rng.choice(_CHANNELS),
        person=rng.choice(_PEOPLE),
        ticket=rng.randint(100, 999),
    )


def _make_ocr_text(rng: random.Random) -> str:
    """Build a ~30-char OCR snippet. Includes ``[DEMO]`` prefix for clarity."""
    template = rng.choice(_OCR_TEMPLATES)
    body = template.format(
        topic=rng.choice(_TOPICS),
        ticket=rng.randint(10, 999),
    )
    return f"[DEMO] {body}"


def _make_phash(rng: random.Random) -> str:
    """Generate a fake 16-hex-char perceptual hash. Format matches real rows."""
    return f"{rng.getrandbits(64):016x}"


def _make_process_name(app_name: str) -> str:
    """Map the friendly app name to a plausible executable name."""
    return {
        "VSCode": "Code.exe",
        "Chrome": "chrome.exe",
        "Slack": "slack.exe",
        "Figma": "Figma.exe",
        "Linear": "Linear.exe",
        "Notion": "Notion.exe",
        "Zoom": "Zoom.exe",
        "Terminal": "pwsh.exe",
        "Mail": "outlook.exe",
        "Spotify": "Spotify.exe",
    }.get(app_name, "unknown.exe")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def count_demo_rows() -> dict[str, int]:
    """Return how many demo rows currently live in each affected table.

    Used by the admin page to render the "currently seeded" badge so the
    operator knows whether to hit Seed or Purge.
    """
    async with get_connection() as conn:
        shots_cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots WHERE is_demo = 1",
        )
        shots_row = await shots_cur.fetchone()
        notes_cur = await conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM screenshot_notes n
            JOIN screenshots s ON s.id = n.screenshot_id
            WHERE s.is_demo = 1
            """,
        )
        notes_row = await notes_cur.fetchone()
        cards_cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM hourly_card WHERE summary LIKE '[DEMO]%'",
        )
        cards_row = await cards_cur.fetchone()
        pins_cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM daily_pin WHERE pin LIKE '[DEMO]%'",
        )
        pins_row = await pins_cur.fetchone()
    return {
        "shots": int(shots_row["n"]) if shots_row is not None else 0,
        "notes": int(notes_row["n"]) if notes_row is not None else 0,
        "cards": int(cards_row["n"]) if cards_row is not None else 0,
        "pins": int(pins_row["n"]) if pins_row is not None else 0,
    }


async def _count_real_screenshots() -> int:
    """Return the number of NON-demo screenshots currently in the DB."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots WHERE is_demo = 0",
        )
        row = await cursor.fetchone()
    return int(row["n"]) if row is not None else 0


async def seed_demo_data(
    days: int = 7,
    shots_per_day: int = 30,
) -> dict[str, int | str]:
    """Insert ~``days * shots_per_day`` demo screenshots plus sidecars.

    Concretely the function inserts:

      * ``days * shots_per_day`` rows in ``screenshots`` distributed
        across weekdays of the trailing ``days`` calendar days, all
        tagged ``is_demo = 1``,
      * ~30 rows in ``screenshot_notes`` against a random subset of the
        freshly-inserted shot ids,
      * 5 rows in ``hourly_card`` spanning the most recent five working
        hours,
      * 1 ``daily_pin`` row for today.

    Returns a dict shaped::

        {
            "inserted_shots": int,
            "inserted_notes": int,
            "inserted_cards": int,
            "inserted_pins": int,
            "day_range": "YYYY-MM-DD..YYYY-MM-DD",
        }

    Raises :class:`SeederRefused` if the screenshots table already has
    more than :data:`_MAX_REAL_ROWS_FOR_SAFETY` real rows — see the
    module docstring for the rationale.
    """
    days = max(1, min(_MAX_DAYS, int(days)))
    shots_per_day = max(1, min(_MAX_SHOTS_PER_DAY, int(shots_per_day)))

    real_rows = await _count_real_screenshots()
    if real_rows > _MAX_REAL_ROWS_FOR_SAFETY:
        log.warning(
            "demo_seeder.refused",
            real_rows=real_rows,
            limit=_MAX_REAL_ROWS_FOR_SAFETY,
        )
        msg = (
            f"Refusing to seed: screenshots table already has {real_rows} "
            f"real rows (limit {_MAX_REAL_ROWS_FOR_SAFETY}). Purge demo data "
            "or wipe the database first."
        )
        raise SeederRefused(msg)

    rng = random.Random(_RANDOM_SEED)  # noqa: S311 — non-cryptographic by design.
    now = datetime.now(tz=UTC)
    timestamps = _spread_timestamps(rng, days, shots_per_day, now=now)

    inserted_shot_ids: list[int] = []
    async with get_connection() as conn:
        for ts in timestamps:
            app_name = rng.choice(_APPS)
            window_title = _make_window_title(rng, app_name)
            ocr_text = _make_ocr_text(rng)
            cursor = await conn.execute(
                """
                INSERT INTO screenshots (
                    captured_at,
                    monitor_index,
                    width,
                    height,
                    thumbnail_path,
                    phash,
                    app_name,
                    window_title,
                    process_name,
                    ocr_status,
                    ocr_text,
                    is_demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(ts),
                    0,
                    _DEFAULT_WIDTH,
                    _DEFAULT_HEIGHT,
                    None,  # thumbnail_path: NULL — no actual file on disk.
                    _make_phash(rng),
                    app_name,
                    window_title,
                    _make_process_name(app_name),
                    "done",
                    ocr_text,
                    1,
                ),
            )
            row_id = cursor.lastrowid
            if row_id is None:
                msg = "INSERT into screenshots returned no row id"
                raise RuntimeError(msg)
            inserted_shot_ids.append(int(row_id))

        inserted_notes = await _seed_notes(conn, rng, inserted_shot_ids)
        inserted_cards = await _seed_hourly_cards(conn, rng, now=now)
        inserted_pins = await _seed_daily_pin(conn, rng, now=now)

        await conn.commit()

    day_range = _format_day_range(timestamps)
    result: dict[str, int | str] = {
        "inserted_shots": len(inserted_shot_ids),
        "inserted_notes": inserted_notes,
        "inserted_cards": inserted_cards,
        "inserted_pins": inserted_pins,
        "day_range": day_range,
    }
    log.info(
        "demo_seeder.seeded",
        shots=result["inserted_shots"],
        notes=result["inserted_notes"],
        cards=result["inserted_cards"],
        pins=result["inserted_pins"],
        day_range=day_range,
    )
    return result


async def purge_demo_data() -> dict[str, int]:
    """Delete every row tagged as demo across the four affected tables.

    Returns a dict with the per-table delete counts so the route can
    surface "purged N rows" to the operator. Safe to call when there is
    nothing to purge (returns zeros).

    Order matters: notes have a ``REFERENCES screenshots`` foreign key
    with ``ON DELETE CASCADE`` (see migration ``002_notes.sql``), so
    deleting the parent screenshots removes the notes implicitly — but
    we issue the explicit DELETE first to get an accurate count *before*
    the cascade fires.
    """
    async with get_connection() as conn:
        notes_cur = await conn.execute(
            """
            DELETE FROM screenshot_notes
            WHERE screenshot_id IN (
                SELECT id FROM screenshots WHERE is_demo = 1
            )
            """,
        )
        deleted_notes = notes_cur.rowcount or 0

        shots_cur = await conn.execute(
            "DELETE FROM screenshots WHERE is_demo = 1",
        )
        deleted_shots = shots_cur.rowcount or 0

        cards_cur = await conn.execute(
            "DELETE FROM hourly_card WHERE summary LIKE '[DEMO]%'",
        )
        deleted_cards = cards_cur.rowcount or 0

        pins_cur = await conn.execute(
            "DELETE FROM daily_pin WHERE pin LIKE '[DEMO]%'",
        )
        deleted_pins = pins_cur.rowcount or 0

        await conn.commit()

    result = {
        "deleted_shots": int(deleted_shots),
        "deleted_notes": int(deleted_notes),
        "deleted_cards": int(deleted_cards),
        "deleted_pins": int(deleted_pins),
    }
    log.info(
        "demo_seeder.purged",
        shots=result["deleted_shots"],
        notes=result["deleted_notes"],
        cards=result["deleted_cards"],
        pins=result["deleted_pins"],
    )
    return result


# ---------------------------------------------------------------------------
# Sidecar seeders (called only from inside :func:`seed_demo_data` so they
# share the same transaction).
# ---------------------------------------------------------------------------


async def _seed_notes(
    conn: aiosqlite.Connection,
    rng: random.Random,
    shot_ids: list[int],
) -> int:
    """Attach ~30 free-text notes to a random subset of ``shot_ids``."""
    if not shot_ids:
        return 0
    target = min(30, len(shot_ids))
    chosen = rng.sample(shot_ids, k=target)
    inserted = 0
    for shot_id in chosen:
        body = rng.choice(_NOTE_BODIES)
        await conn.execute(
            """
            INSERT OR REPLACE INTO screenshot_notes (screenshot_id, body)
            VALUES (?, ?)
            """,
            (int(shot_id), body),
        )
        inserted += 1
    return inserted


async def _seed_hourly_cards(
    conn: aiosqlite.Connection,
    rng: random.Random,
    *,
    now: datetime,
) -> int:
    """Insert 5 hourly cards covering the trailing five whole hours."""
    inserted = 0
    # Start from the most recently completed hour (now floored to hour
    # minus 1) and walk back five steps. ``hour_start`` is PRIMARY KEY in
    # ``hourly_card`` so we use INSERT OR IGNORE — re-running the seeder
    # never blows up on a duplicate.
    floored = now.replace(minute=0, second=0, microsecond=0)
    for step in range(1, 6):
        hour_start = floored - timedelta(hours=step)
        hour_end = hour_start + timedelta(minutes=59, seconds=59)
        summary = (
            f"[DEMO] Hour {hour_start:%H:00}: focused on "
            f"{rng.choice(_TOPICS)}; reviewed PRs and notes."
        )
        top_words = rng.choice(_TOP_WORDS_PER_HOUR)
        await conn.execute(
            """
            INSERT OR IGNORE INTO hourly_card (
                hour_start,
                hour_end,
                summary,
                apps_json,
                screen_count,
                audio_seconds,
                top_words,
                transcript_excerpt,
                llm_enriched
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(hour_start),
                _iso(hour_end),
                summary,
                '[{"app":"VSCode","minutes":42},{"app":"Chrome","minutes":18}]',
                rng.randint(40, 90),
                rng.randint(0, 600),
                top_words,
                "[DEMO] No real transcript — synthetic demo card.",
                0,
            ),
        )
        inserted += 1
    return inserted


async def _seed_daily_pin(
    conn: aiosqlite.Connection,
    rng: random.Random,
    *,
    now: datetime,
) -> int:
    """Insert one daily pin for today (idempotent on the PK ``day``)."""
    pin_body = "[DEMO] " + rng.choice(_PIN_TEMPLATES).format(person=rng.choice(_PEOPLE))
    apps = ",".join(rng.sample(_APPS, k=5))
    await conn.execute(
        """
        INSERT OR REPLACE INTO daily_pin (
            day, pin, apps, voice_minutes, screen_count, source
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            now.date().isoformat(),
            pin_body,
            apps,
            rng.randint(0, 45),
            rng.randint(120, 480),
            "heuristic",
        ),
    )
    return 1


def _format_day_range(timestamps: Iterable[datetime]) -> str:
    """Render ``YYYY-MM-DD..YYYY-MM-DD`` for the inserted span."""
    timestamps = list(timestamps)
    if not timestamps:
        return "(empty)"
    first = timestamps[0].date().isoformat()
    last = timestamps[-1].date().isoformat()
    if first == last:
        return first
    return f"{first}..{last}"


__all__ = [
    "SeederRefused",
    "count_demo_rows",
    "purge_demo_data",
    "seed_demo_data",
]
