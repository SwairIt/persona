"""Whole-site functional smoke: every GET route, every role, no 500s.

Why this file exists
--------------------
The owner surface is roughly 400 pages accumulated over months, and the rest
of the suite never opens most of them: ``test_mvp_smoke_audit`` walks the
*member* surface, ``test_member_data_isolation_audit`` checks leakage, and the
per-feature tests each open the one page they own. Nothing walked the owner's
own pages, so a page could 500 for months without a single test noticing. That
is exactly what had happened — the first run of this walk found five hard
500s, every one of them permanent (a phantom SQL column, a Jinja name
collision, a latin-1 header crash), i.e. those endpoints had *never* worked:

* ``/stats/top100`` — the route handed the template a dict keyed ``items``;
  Jinja resolves ``column.items`` with ``getattr`` first and got the bound
  ``dict.items`` method.
* ``/export/screenshots.csv``, ``/feeds/pinned.rss`` — both selected
  ``screenshots.pinned_at``, a column that does not exist (pinning is the
  ``tier = 'pinned'`` enum).
* ``/export/audio-segments.csv`` — selected ``started_at`` / ``duration_s``;
  the real columns are ``captured_at`` / ``duration_seconds``.
* ``/export/tag/{tag}/ocr.txt`` — put a Cyrillic tag name into a
  ``Content-Disposition`` header, which is latin-1.
* ``/tags/tree``, ``/searches/facets``, ``/share/insights`` — literal paths
  registered *after* a pattern that matches them (``/tags/{tag_id}`` etc.),
  so they answered 422 / 404 / 403 instead of rendering.

What it guarantees
------------------
For anonymous, a fresh member and the owner, every GET route the application
registers either answers or redirects — never 5xx, never an unhandled
exception. Path parameters are filled from a small seeded dataset so
``/screenshot/{id}``, ``/day/{date}``, ``/chat/{id}`` render real content
rather than a 404 that would hide a template bug.

What it deliberately does NOT do
--------------------------------
* **Streaming endpoints** (:data:`STREAMING_PATHS`) are skipped: they hold the
  connection open by design, so "does it 500" is not answerable with a GET.
* **Slow local-probe endpoints** (:data:`SLOW_PATHS`) are skipped: they walk
  the host's process table or dial an Ollama endpoint with a 4s timeout each.
  They are covered by their own tests.
* **Destructive owner actions are never executed as the owner.** They are
  exercised only as a *member*, to prove the gate holds — see
  :func:`test_destructive_endpoints_are_owner_gated`. Running them as the
  owner would prove nothing this suite needs and would make the test a
  loaded gun if it ever ran against a real data directory.
* Path params whose name this file cannot map to seeded data are reported by
  :func:`test_parameterised_route_coverage_stays_high` rather than silently
  dropped, so coverage cannot rot unnoticed.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import get_kv, set_kv
from app.web import templates_engine
from app.web.main import create_app
from app.web.middleware import auth_gate
from app.web.routes import setup_gate

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = REPO_ROOT / "app" / "web" / "static"

#: Long-lived streams. A GET never completes, so they cannot be smoke-tested
#: this way; each has its own test.
STREAMING_PATHS: frozenset[str] = frozenset(
    {
        "/events",
        "/api/notifications/stream",
    }
)

#: Endpoints that probe the *host* rather than the database: a full psutil
#: process walk, or two 4-second HTTP calls to a possibly-absent Ollama.
#: Correct but slow; skipping them keeps this walk practical.
SLOW_PATHS: frozenset[str] = frozenset(
    {
        "/app-icon/{app_name}.png",
        "/icons/{process_name}.png",
        "/settings/system-monitor",
        "/api/system-monitor.json",
    }
)

#: Per-request ceiling. Anything slower than this is a bug in its own right;
#: we record it as a failure rather than hanging the suite.
REQUEST_TIMEOUT_SECONDS = 20.0

#: Floor for how many parameterised routes must be reachable with seeded data.
#: Currently 100% of them are. The floor sits just below that so a couple of
#: new routes can land before someone has to map their path parameter — not so
#: low that the walk could quietly stop testing a whole feature. Raise it when
#: you add seed data; never lower it to make a failure go away.
MIN_PARAM_ROUTE_COVERAGE = 0.95


def _reset_caches() -> None:
    """Drop every process-global identity / settings cache.

    Same reset as ``test_mvp_smoke_audit``: each test gets a fresh database but
    the same interpreter, and the theme cache is a ContextVar that survives the
    test boundary.
    """
    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    owner._fa_cache["value"] = None
    owner._fa_cache["checked_at"] = 0.0
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._role_gate_cache["value"] = False
    auth_gate._role_gate_cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0
    templates_engine._kv_value_cache.clear()
    templates_engine._user_kv_value_cache.clear()
    templates_engine.invalidate_theme_cache()
    i18n.invalidate_language_cache()


# ---------------------------------------------------------------------------
# Seed — one small but realistic row per surface the URL space addresses
# ---------------------------------------------------------------------------

DAY = "2026-08-20"
HOUR_START = f"{DAY}T10:00:00"
TAG_NAME = "работа"  # deliberately Cyrillic: it caught the latin-1 header bug


async def _seed(owner_id: int, member_id: int) -> dict[str, Any]:
    """Insert one row per addressable entity and return the ids to fill URLs with.

    Kept deliberately small — the point is that ``/screenshot/{id}`` renders a
    *real* screenshot, not that the tables are full. Each optional insert is
    isolated so a schema change to one feature cannot blank the whole walk.
    """
    seed: dict[str, Any] = {}
    async with get_connection() as conn:
        cur = await conn.execute(
            "INSERT INTO screenshots (captured_at, monitor_index, width, height, "
            "phash, app_name, window_title, ocr_text, ocr_status, thumbnail_path, "
            "tier, created_at) VALUES (?, 0, 1920, 1080, ?, ?, ?, ?, 'done', ?, "
            "'pinned', datetime('now'))",
            (
                f"{DAY}T10:15:00",
                "abc123def456",
                "Telegram",
                "Окно проекта",
                "проектный документ persona текст",
                "thumbs/probe.jpg",
            ),
        )
        seed["screenshot_id"] = cur.lastrowid
        await conn.execute(
            "INSERT INTO ocr_word (screenshot_id, word, conf, left, top, width, "
            "height) VALUES (?, 'persona', 90, 10, 10, 50, 20)",
            (seed["screenshot_id"],),
        )
        cur = await conn.execute(
            "INSERT INTO notes (body, created_at, updated_at) "
            "VALUES ('Заметка про проект', datetime('now'), datetime('now'))"
        )
        seed["note_id"] = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO tags (name, created_at) VALUES (?, datetime('now'))",
            (TAG_NAME,),
        )
        seed["tag_id"] = cur.lastrowid
        await conn.execute(
            "INSERT INTO screenshot_tags (screenshot_id, tag_id) VALUES (?, ?)",
            (seed["screenshot_id"], seed["tag_id"]),
        )
        cur = await conn.execute(
            "INSERT INTO reminders (body, due_date) VALUES ('Позвонить', ?)",
            (f"{DAY}T09:00:00",),
        )
        seed["reminder_id"] = cur.lastrowid
        await conn.execute(
            "INSERT INTO hourly_card (hour_start, hour_end, summary, apps_json, "
            "screen_count, audio_seconds, top_words, transcript_excerpt, "
            "llm_enriched, created_at) VALUES (?, ?, 'Работал над Persona', ?, 12, "
            "300, 'persona,проект', 'речь про проект', 0, datetime('now'))",
            (HOUR_START, f"{DAY}T10:59:59", '[{"app": "Telegram", "count": 12}]'),
        )
        cur = await conn.execute(
            "INSERT INTO chat_session (user_id, title, created_at, updated_at, "
            "summary_up_to_id, auto_switch_on_image) "
            "VALUES (?, 'Первый чат', datetime('now'), datetime('now'), 0, 0)",
            (owner_id,),
        )
        seed["session_id"] = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chat_message (session_id, role, content, created_at, "
            "is_streaming, is_pinned, access_count) "
            "VALUES (?, 'user', 'Привет', datetime('now'), 0, 0, 0)",
            (seed["session_id"],),
        )
        seed["message_id"] = cur.lastrowid
        await conn.execute(
            "INSERT INTO chat_session (user_id, title, created_at, updated_at, "
            "summary_up_to_id, auto_switch_on_image) "
            "VALUES (?, 'Чат участника', datetime('now'), datetime('now'), 0, 0)",
            (member_id,),
        )
        cur = await conn.execute(
            "INSERT INTO audio_segment (captured_at, ended_at, duration_seconds, "
            "codec, path, size_bytes, transcript) "
            "VALUES (?, ?, 60.0, 'opus', '/tmp/a.opus', 1024, 'речь про проект')",
            (f"{DAY}T10:20:00", f"{DAY}T10:21:00"),
        )
        seed["audio_id"] = cur.lastrowid
        await conn.commit()

    optional: tuple[tuple[str, str], ...] = (
        (
            "saved_search_slug",
            "INSERT INTO saved_search (slug, title, query, created_at) "
            "VALUES ('probe', 'мой поиск', 'persona', datetime('now'))",
        ),
        (
            "saved_searches_id",
            "INSERT INTO saved_searches (name, query, created_at) "
            "VALUES ('мой поиск', 'persona', datetime('now'))",
        ),
        (
            "device_id",
            "INSERT INTO device (user_id, name, kind, device_token, created_at) "
            f"VALUES ({owner_id}, 'mac', 'mac', 'probe-device-token', "
            "datetime('now'))",
        ),
        (
            "collection_slug",
            "INSERT INTO auto_collection (slug, title, tag, public, created_at) "
            f"VALUES ('probe', 'коллекция', '{TAG_NAME}', 1, datetime('now'))",
        ),
        (
            "memory_id",
            "INSERT INTO user_memory (user_id, kind, text, created_at, updated_at) "
            f"VALUES ({owner_id}, 'fact', 'Живёт в Москве', datetime('now'), "
            "datetime('now'))",
        ),
        (
            "entity_id",
            "INSERT INTO entity (name, kind, first_seen, last_seen, mention_count) "
            "VALUES ('Persona', 'project', datetime('now'), datetime('now'), 1)",
        ),
        (
            "permalink_slug",
            "INSERT INTO permalink (slug, target_url, label, created_at) "
            "VALUES ('probe', '/timeline', 'проба', datetime('now'))",
        ),
        (
            "skill_id",
            "INSERT INTO skill (user_id, name, content, enabled, created_at) "
            f"VALUES ({owner_id}, 'проба', 'делай добро', 1, datetime('now'))",
        ),
        (
            "focus_id",
            "INSERT INTO focus_profile (name, created_at) "
            "VALUES ('глубокая работа', datetime('now'))",
        ),
    )
    for key, sql in optional:
        try:
            async with get_connection() as conn:
                cur = await conn.execute(sql)
                seed[key] = cur.lastrowid
                await conn.commit()
        except Exception:  # noqa: BLE001 — an optional surface must not sink the walk
            continue
    return seed


@pytest_asyncio.fixture
async def site():
    """The real application, an owner, a member and a seeded dataset.

    ``owner_exclusive_mode`` is OFF: that kill-switch is what a locked-down
    deployment sets, and this walk is about the *open* deployment.
    """
    await init_database()
    owner_user = await create_user("owner@sitesmoke.test", "Zq7-frost-lantern-91")
    member_user = await create_user("member@sitesmoke.test", "Kp4-velvet-harbour-38")
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner_user["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
        await conn.commit()
    seed = await _seed(owner_user["id"], member_user["id"])
    setup_gate._cache.mark_done()
    _reset_caches()

    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as client:
            yield client, owner_user, member_user, seed
    finally:
        _reset_caches()


async def _login(client: AsyncClient, uid: int | None) -> None:
    client.cookies.clear()
    if uid is None:
        return
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


# ---------------------------------------------------------------------------
# Route enumeration + URL filling
# ---------------------------------------------------------------------------


def get_route_paths() -> list[str]:
    """Every GET path the application registers, taken from the app itself.

    Read from ``create_app()`` rather than hardcoded, so a route added
    tomorrow is walked tomorrow without anyone remembering to list it.
    """
    app = create_app()
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path and "GET" in methods:
            paths.add(path)
    return sorted(paths)


_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def _param_values(seed: dict[str, Any]) -> dict[str, Any]:
    """Map a path-parameter *name* to a value that addresses seeded data."""
    shot = seed.get("screenshot_id")
    return {
        # screenshots
        "screenshot_id": shot,
        "shot_id": shot,
        "id": shot,
        "id_a": shot,
        "id_b": shot,
        "a": shot,
        "b": shot,
        # other entities
        "note_id": seed.get("note_id"),
        "att_id": seed.get("note_id"),
        "tag_id": seed.get("tag_id"),
        "tag": TAG_NAME,
        "tag_name": TAG_NAME,
        "name": TAG_NAME,
        "reminder_id": seed.get("reminder_id"),
        "session_id": seed.get("session_id"),
        "thread_id": seed.get("session_id"),
        "message_id": seed.get("message_id"),
        "seg_id": seed.get("audio_id"),
        "audio_id": seed.get("audio_id"),
        "segment_id": seed.get("audio_id"),
        "sketch_id": seed.get("note_id"),
        "recycle_id": seed.get("note_id"),
        "device_id": seed.get("device_id"),
        "entity_id": seed.get("entity_id"),
        "memory_id": seed.get("memory_id"),
        "search_id": seed.get("saved_searches_id"),
        "skill_id": seed.get("skill_id"),
        "profile_id": seed.get("focus_id"),
        "user_id": 1,
        "uid": 1,
        "group_id": 1,
        "telegram_user_id": 1,
        # time
        "date": DAY,
        "day": DAY,
        "day_iso": DAY,
        "date_str": DAY,
        "iso_date": DAY,
        "ymd": DAY.replace("-", ""),
        "iso": f"{DAY}T10",
        "hour": f"{DAY}T10",
        "month": DAY[:7],
        "week": "2026-W34",
        "week_start": "2026-08-17",
        "week_start_iso": "2026-08-17",
        "year": DAY[:4],
        # strings
        "app": "Telegram",
        "app_name": "Telegram",
        "process_name": "Telegram",
        "query": "persona",
        "q": "persona",
        "kind": "apps",
        "what": "ics",
        "lang": "ru",
        "token": "probe-device-token",
        "slug": "probe",
        "key": "probe",
        "filename": "probe.jpg",
        "path": "probe.jpg",
        "relative_path": "probe.jpg",
    }


def fill_path(path: str, seed: dict[str, Any]) -> str | None:
    """Substitute path params from seeded data. ``None`` when a name is unknown."""
    table = _param_values(seed)
    unknown = False

    def repl(match: re.Match[str]) -> str:
        nonlocal unknown
        name = match.group(1)
        value = table.get(name)
        if value is None:
            value = table.get(name.rstrip("s"))
        if value is None:
            unknown = True
            return ""
        return str(value)

    filled = _PARAM_RE.sub(repl, path)
    return None if unknown else filled


def walkable_paths() -> list[str]:
    """GET routes this walk can meaningfully exercise."""
    return [
        p
        for p in get_route_paths()
        if p not in STREAMING_PATHS and p not in SLOW_PATHS
    ]


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


async def _walk(
    client: AsyncClient,
    seed: dict[str, Any],
) -> tuple[list[str], list[tuple[str, int]]]:
    """GET every walkable route once. Returns ``(failures, results)``."""
    failures: list[str] = []
    results: list[tuple[str, int]] = []
    for path in walkable_paths():
        target = fill_path(path, seed)
        if target is None:
            continue
        try:
            response = await asyncio.wait_for(
                client.get(target, follow_redirects=False),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            failures.append(
                f"{path} → no response in {REQUEST_TIMEOUT_SECONDS:.0f}s "
                "(a page nobody can load is as broken as a 500)"
            )
            continue
        except Exception as exc:  # noqa: BLE001 — an unhandled error IS the finding
            failures.append(f"{path} → unhandled {type(exc).__name__}: {exc}")
            continue
        results.append((path, response.status_code))
        # 503 is exempt on purpose and ONLY 503: it is what a route returns
        # when an OPTIONAL backend is absent (no reportlab/weasyprint → no PDF
        # export). That is a deliberate, self-explaining degradation, not a
        # crash — an unhandled error surfaces as 500. See
        # :func:`test_pdf_endpoints_degrade_instead_of_crashing`, which pins
        # the behaviour so "503 is fine" cannot quietly cover a real failure.
        if response.status_code >= 500 and response.status_code != 503:
            failures.append(
                f"{path} → {response.status_code}: {response.text[:300]}"
            )
    return failures, results


@pytest.mark.asyncio
async def test_no_route_500s_for_anonymous(site: Any) -> None:
    client, _owner_user, _member_user, seed = site
    await _login(client, None)
    failures, results = await _walk(client, seed)
    assert results, "walked nothing — enumeration broke"
    assert not failures, "anonymous hits a broken route:\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_no_route_500s_for_member(site: Any) -> None:
    client, _owner_user, member_user, seed = site
    await _login(client, member_user["id"])
    failures, results = await _walk(client, seed)
    assert results, "walked nothing — enumeration broke"
    assert not failures, "a member hits a broken route:\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_no_route_500s_for_owner(site: Any) -> None:
    """The big one: the ~400-page owner surface nothing else opens."""
    client, owner_user, _member_user, seed = site
    await _login(client, owner_user["id"])
    failures, results = await _walk(client, seed)
    assert results, "walked nothing — enumeration broke"
    assert not failures, "the owner hits a broken route:\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_owner_key_pages_actually_render(site: Any) -> None:
    """A 303 to /landing would pass the "no 500" bar. These must be real 200s.

    One page per major owner surface — if any of these stops rendering, the
    product is visibly broken even though the walk above stays green.
    """
    client, owner_user, _member_user, seed = site
    await _login(client, owner_user["id"])

    must_render = (
        "/now",
        "/timeline",
        "/search",
        # NB: there is no ``/notes`` index route — the notes surface is
        # ``/journal`` + ``/inbox`` + ``/notes/day/{day}``. ``/notes`` appears
        # in test_mvp_smoke_audit's owner-only probe list, where a 404 counts
        # as "not a leak", which is why nobody noticed it isn't a page.
        "/journal",
        "/inbox",
        f"/notes/day/{DAY}",
        "/reminders",
        "/dashboard",
        "/analytics",
        "/stats",
        "/stats/top100",
        "/tags",
        "/tags/tree",
        "/searches/facets",
        "/share/insights",
        "/memory",
        "/graph",
        "/briefing",
        "/thoughts",
        "/devices",
        "/settings/hub",
        "/settings/capture",
        "/root",
        f"/day/{DAY}",
        f"/screenshot/{seed['screenshot_id']}",
    )
    broken: list[str] = []
    for path in must_render:
        response = await client.get(path, follow_redirects=False)
        if response.status_code != 200:
            broken.append(f"{path} → {response.status_code} {response.text[:200]}")
    assert not broken, "owner pages that no longer render:\n" + "\n".join(broken)


# ---------------------------------------------------------------------------
# Route table shape — cheap checks that need no HTTP at all
# ---------------------------------------------------------------------------


def test_no_literal_route_is_shadowed_by_an_earlier_pattern() -> None:
    """A literal path registered after a pattern that matches it is unreachable.

    Starlette matches in registration order. ``/tags/tree`` sat ~1000 routes
    after ``/tags/{tag_id}`` and answered 422 ("unable to parse 'tree' as
    integer"); ``/share/insights`` answered 403 from ``/share/{token}``; and
    ``/searches/facets`` was eaten by ``/searches/{slug}``. All three were
    live, linked pages that had simply never worked. The failure mode is
    invisible in the source — only the assembled route table shows it.
    """
    app = create_app()
    routes = [
        r
        for r in app.routes
        if getattr(r, "path", None) and "GET" in (getattr(r, "methods", None) or set())
    ]
    shadowed: list[str] = []
    for index, route in enumerate(routes):
        if "{" in route.path:
            continue
        for earlier in routes[:index]:
            if "{" not in earlier.path:
                continue
            if earlier.path_regex.match(route.path):
                shadowed.append(f"{route.path} is shadowed by {earlier.path}")
                break
    assert not shadowed, (
        "unreachable literal route(s) — include the literal's router BEFORE "
        "the pattern's in app/web/main.py:\n" + "\n".join(shadowed)
    )


def test_no_route_is_registered_twice() -> None:
    """Two includes of the same router double every one of its routes.

    Harmless-looking, but it doubles the OpenAPI schema and makes the shadowing
    check above ambiguous. main.py already carries three comments about routers
    that were registered twice; this makes the fourth one fail instead.
    """
    app = create_app()
    seen: dict[tuple[str, tuple[str, ...]], int] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        key = (path, tuple(sorted(methods)))
        seen[key] = seen.get(key, 0) + 1
    duplicates = [f"{p} {m}" for (p, m), n in seen.items() if n > 1]
    assert not duplicates, "route registered more than once:\n" + "\n".join(duplicates)


def test_parameterised_route_coverage_stays_high() -> None:
    """Most parameterised routes must be reachable with the seeded dataset.

    A route whose ``{param}`` name this file cannot map is silently skipped by
    the walk — so "all green" could mean "we tested almost nothing". This puts
    a floor under it. When it fails, add the parameter name to
    :func:`_param_values` (and seed a row if there isn't one), don't lower the
    floor.
    """
    seed = {
        key: 1
        for key in (
            "screenshot_id",
            "note_id",
            "tag_id",
            "reminder_id",
            "session_id",
            "message_id",
            "audio_id",
            "device_id",
            "entity_id",
            "memory_id",
            "saved_searches_id",
            "skill_id",
            "focus_id",
        )
    }
    parameterised = [p for p in walkable_paths() if "{" in p]
    unfillable = [p for p in parameterised if fill_path(p, seed) is None]
    covered = 1 - len(unfillable) / len(parameterised)
    assert covered >= MIN_PARAM_ROUTE_COVERAGE, (
        f"only {covered:.0%} of parameterised routes can be addressed "
        f"(floor {MIN_PARAM_ROUTE_COVERAGE:.0%}). Unmapped path params:\n"
        + "\n".join(sorted(unfillable))
    )


# ---------------------------------------------------------------------------
# Exports — a 200 with an empty body is a broken download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exports_return_a_real_body_of_the_right_type(site: Any) -> None:
    """Every export must produce non-empty bytes with its declared content type.

    ``/export/screenshots.csv``, ``/export/audio-segments.csv`` and
    ``/feeds/pinned.rss`` each 500'd on a column that does not exist, so this
    is the regression guard for exactly that: not "did it answer" but "did it
    hand back a file".
    """
    client, owner_user, _member_user, _seed = site
    await _login(client, owner_user["id"])

    expected: tuple[tuple[str, str], ...] = (
        ("/export/screenshots.csv", "text/csv"),
        ("/export/audio-segments.csv", "text/csv"),
        ("/export/hourly-cards.csv", "text/csv"),
        ("/export/notes.csv", "text/csv"),
        (f"/export/tag/{TAG_NAME}/ocr.txt", "text/plain"),
        (f"/export/ocr.txt?day={DAY}", "text/plain"),
        (f"/export/day/{DAY}.md", "text/markdown"),
        ("/feeds/pinned.rss", "application/rss+xml"),
        ("/api/export/full.zip", "application/zip"),
        ("/admin/diagnostics-bundle.zip", "application/zip"),
    )
    problems: list[str] = []
    for path, content_type in expected:
        response = await client.get(path, follow_redirects=False)
        if response.status_code != 200:
            problems.append(f"{path} → {response.status_code} {response.text[:200]}")
            continue
        actual = response.headers.get("content-type", "")
        if content_type not in actual:
            problems.append(f"{path} → content-type {actual!r}, wanted {content_type!r}")
        if not response.content:
            problems.append(f"{path} → 200 but zero bytes (a broken download)")
    assert not problems, "export endpoints:\n" + "\n".join(problems)


@pytest.mark.asyncio
async def test_csv_exports_carry_the_seeded_row(site: Any) -> None:
    """Not just a header line — the export must actually contain the data.

    Both broken CSVs would have passed a header-only check after the SQL was
    "fixed" to select nothing.
    """
    client, owner_user, _member_user, _seed = site
    await _login(client, owner_user["id"])

    shots = await client.get("/export/screenshots.csv")
    assert "Telegram" in shots.text, shots.text[:400]
    # pinned_at is derived from tier='pinned'; the seeded shot is pinned.
    assert "pinned_at" in shots.text.splitlines()[0]

    audio = await client.get("/export/audio-segments.csv")
    assert "opus" in audio.text, audio.text[:400]

    feed = await client.get("/feeds/pinned.rss")
    assert "<item>" in feed.text, "pinned RSS lost its only pinned shot"


#: Endpoints that need an optional PDF backend (reportlab / weasyprint).
#: Neither is a declared dependency, so on a stock install these are the only
#: routes in the whole app that legitimately answer 5xx.
PDF_ENDPOINTS: tuple[str, ...] = (
    "/export/pdf",
    "/export/weekly-pdf",
    "/apps/Telegram/digest.pdf",
    "/collection/probe/export.pdf",
)


@pytest.mark.asyncio
async def test_pdf_endpoints_degrade_instead_of_crashing(site: Any) -> None:
    """PDF routes must answer 200 (backend present) or an explained 503.

    The walk above exempts 503, so this is what stops that exemption from
    hiding a genuine failure: a 503 here has to name the missing backend, and
    anything else — a 500, a blank body — fails.
    """
    client, owner_user, _member_user, _seed = site
    await _login(client, owner_user["id"])

    problems: list[str] = []
    for path in PDF_ENDPOINTS:
        response = await client.get(path, follow_redirects=False)
        if response.status_code == 200:
            if not response.content:
                problems.append(f"{path} → 200 with an empty body")
            continue
        if response.status_code != 503:
            problems.append(f"{path} → {response.status_code}: {response.text[:200]}")
            continue
        body = response.text.lower()
        if not any(name in body for name in ("reportlab", "weasyprint", "pdf")):
            problems.append(
                f"{path} → 503 that does not say what is missing: {response.text[:200]}"
            )
    assert not problems, "PDF endpoints:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# Owner writes + destructive-action gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_settings_post_persists_to_kv(site: Any) -> None:
    """A settings form that answers 303 but writes nothing is a silent no-op."""
    client, owner_user, _member_user, _seed = site
    await _login(client, owner_user["id"])

    response = await client.post(
        "/settings/theme", data={"theme": "cosmos"}, follow_redirects=False
    )
    assert response.status_code in (200, 303), response.text[:300]
    async with get_connection() as conn:
        stored = await get_kv(conn, "theme")
    assert stored == "cosmos", f"theme POST did not reach kv (got {stored!r})"


#: Owner-only endpoints that destroy data. NEVER invoked as the owner — see the
#: module docstring. Each is called as a *member* to prove the gate holds.
DESTRUCTIVE_ENDPOINTS: tuple[tuple[str, dict[str, str]], ...] = (
    ("/ocr-admin/reset-all", {}),
    ("/ocr-admin/reset-failed", {}),
    ("/ocr-admin/reset-skipped", {}),
    ("/recycle/purge-all", {}),
    ("/settings/privacy/wipe-memory", {}),
    ("/api/demo-seeder/purge", {}),
    ("/api/stale-notes/prune", {}),
    ("/api/bulk/delete-by-app", {"app_name": "Telegram"}),
    ("/api/bulk/delete-by-range", {"since": "2020-01-01", "until": "2030-01-01"}),
    ("/admin/bulk-delete/confirm", {"query": "persona"}),
    ("/settings/app-retention/Telegram/delete", {}),
    ("/settings/system-prompt/reset", {}),
    ("/settings/digest-prompt/reset", {}),
)


@pytest.mark.asyncio
async def test_destructive_endpoints_are_owner_gated(site: Any) -> None:
    """A member must never be able to fire a data-destroying owner action.

    Asserted from the member side only: the gate either 403s the JSON APIs or
    redirects the pages to ``/chat``. A 2xx here would mean a registered
    stranger can wipe the owner's capture history. The owner's own ability to
    run these is deliberately NOT exercised.
    """
    client, _owner_user, member_user, seed = site
    await _login(client, member_user["id"])

    leaks: list[str] = []
    for path, payload in DESTRUCTIVE_ENDPOINTS:
        response = await client.post(path, data=payload, follow_redirects=False)
        if 200 <= response.status_code < 300:
            leaks.append(f"{path} → {response.status_code} (member executed it!)")
        elif response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if location.startswith(("/now", "/timeline", "/root", "/admin")):
                leaks.append(f"{path} → {response.status_code} {location} (ran, then redirected)")
    assert not leaks, "destructive owner actions reachable by a member:\n" + "\n".join(leaks)

    # Control: the seeded data is still there, i.e. nothing above executed.
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM screenshots")
        (shots,) = await cursor.fetchone()
        cursor = await conn.execute("SELECT COUNT(*) FROM user_memory")
        (facts,) = await cursor.fetchone()
    assert shots == 1, f"a destructive endpoint ran: {shots} screenshots left"
    assert facts >= 1, "a destructive endpoint wiped user memory"
    assert seed["screenshot_id"] is not None


# ---------------------------------------------------------------------------
# Static assets — a 404 on a JS file breaks a page without any HTTP error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_static_asset_a_rendered_page_references_exists(site: Any) -> None:
    """Parse rendered owner HTML, resolve every local ``/static/`` reference.

    A missing JS file produces no 500 and no failing status on the page itself
    — the page just silently stops working. The only way to catch it is to read
    what the page actually asked for.
    """
    client, owner_user, _member_user, seed = site
    await _login(client, owner_user["id"])

    # A representative page per shell/layout family. Walking all ~570 pages
    # here would triple the runtime for the same asset set, since almost every
    # page inherits the same base.html.
    sample = (
        "/now",
        "/timeline",
        "/search",
        "/reminders",
        "/dashboard",
        "/analytics",
        "/graph",
        "/memory",
        "/chat",
        "/voice",
        "/inbox",
        "/settings/hub",
        "/settings/capture",
        "/settings/llm",
        "/journal",
        "/root",
        "/help",
        "/landing",
        "/pricing",
        "/blog",
        "/stats/top100",
        f"/day/{DAY}",
        f"/screenshot/{seed['screenshot_id']}",
    )
    reference_re = re.compile(r"""(?:src|href)\s*=\s*["']([^"'>]+)["']""", re.IGNORECASE)

    referenced: dict[str, str] = {}
    for path in sample:
        response = await client.get(path, follow_redirects=True)
        if "text/html" not in response.headers.get("content-type", ""):
            continue
        for raw in reference_re.findall(response.text):
            ref = raw.strip().split("?", 1)[0].split("#", 1)[0]
            if ref.startswith("/static/"):
                referenced.setdefault(ref, path)

    assert len(referenced) >= 20, (
        f"only found {len(referenced)} static references across {len(sample)} "
        "pages — the parse or the sample is broken, not the assets"
    )

    missing: list[str] = []
    for ref, page in sorted(referenced.items()):
        response = await client.get(ref)
        if response.status_code != 200:
            missing.append(f"{ref} → {response.status_code} (referenced by {page})")
        elif not (STATIC_ROOT / ref[len("/static/") :]).exists():
            missing.append(f"{ref} served but absent from {STATIC_ROOT} ({page})")
    assert not missing, "broken static asset reference(s):\n" + "\n".join(missing)
