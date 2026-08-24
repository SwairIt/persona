"""Executable architecture and startup budgets.

The legacy route layer is intentionally migrated incrementally.  These tests
therefore freeze its current direct-database debt while making the new clean
layers strict from their first module.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"

# Exact measured debt before the strangler migration. New paths are forbidden;
# deleting or migrating an existing path is allowed without updating the file.
ROUTE_DIRECT_DB_IMPORT_BASELINE = frozenset(
    line.strip()
    for line in (
        PROJECT_ROOT / "tests" / "architecture_route_db_debt.txt"
    ).read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)

# The reviewed outbound browser worker adds five token-authenticated protocol
# routes to the former 1,081-route surface. Any further growth needs review.
# Reviewed +1 for the consolidated two-phase PC-worker enrollment endpoint.
# Owner ticket issuance reuses /settings/automation, so onboarding adds only
# this single unauthenticated-but-ticket-gated route.
# Reviewed +6 owner-scoped Persona surfaces: Telegram people (2), site copilot
# SSE (1), and adaptive prompt history/toggle/rollback (3).
# Reviewed +2 owner-only writes on the existing /settings/telegram-people page:
# saving a person's display name/note/mute flag, and reassigning Telegram
# ownership. Both are POSTs under a page that already existed; no new surface.
# Reviewed +1: finished month-old orphaned work — POST /api/settings/ai-search
# (owner-gated, AI-everywhere-gated, LLM-optional settings-palette fallback).
# Reviewed +4: the thinking-loop settings page and diary (GET/POST
# /settings/thinking, GET /thoughts, POST /thoughts/{id}/confirm) — all
# owner-gated, surfacing Task 1-4's thinking loop for the first time.
# Reviewed +2: GET/POST /settings/telegram-chats — the owner's UI for
# per-chat reply/read/ignore mode and history-ingest flag (telegram_chat_pref,
# Task 1-2 of the same plan), replacing the old "is it pinned in Telegram"
# accident with an explicit owner choice.
REGISTERED_ROUTE_BUDGET = 1_110  # +6 (2026-08-24): AI-everywhere wave 2 (insights, suggest, hour summary, ai-calendar parse+create)

# After the first bootstrap extraction the reference Windows host imports the
# web app in ~5.3 s. Keep CI headroom while preventing a return to the measured
# 11.6 s legacy baseline. The master-plan target remains <2 s.
COLD_IMPORT_BUDGET_SECONDS = 10.0

_DB_ENTRYPOINTS = frozenset({"get_connection", "write_transaction"})


def _python_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return root.rglob("*.py")


def _imports(path: Path) -> list[tuple[int, str, frozenset[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, frozenset[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                relative = path.relative_to(PROJECT_ROOT).with_suffix("")
                package = ("app", *relative.parts[1:-1])
                keep = max(0, len(package) - node.level + 1)
                prefix = ".".join(package[:keep])
                module = ".".join(part for part in (prefix, module) if part)
            found.append(
                (
                    node.lineno,
                    module,
                    frozenset(alias.name for alias in node.names),
                )
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name, frozenset()))
    return found


def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def test_new_domain_and_application_layers_obey_dependency_rule() -> None:
    """Clean layers depend inward and never acquire framework/infrastructure."""
    rules = {
        APP_ROOT / "domains": (
            "aiosqlite",
            "fastapi",
            "httpx",
            "jinja2",
            "playwright",
            "requests",
            "starlette",
            "telegram",
            "app.adapters",
            "app.application",
            "app.bootstrap",
            "app.entrypoints",
            "app.integrations",
            "app.llm",
            "app.mcp",
            "app.settings",
            "app.storage",
            "app.web",
        ),
        APP_ROOT / "application": (
            "aiosqlite",
            "fastapi",
            "httpx",
            "jinja2",
            "playwright",
            "requests",
            "starlette",
            "telegram",
            "app.adapters",
            "app.bootstrap",
            "app.entrypoints",
            "app.integrations",
            "app.llm",
            "app.mcp",
            "app.settings",
            "app.storage",
            "app.web",
        ),
    }
    violations: list[str] = []
    for layer_root, forbidden in rules.items():
        assert layer_root.is_dir(), f"Required clean layer is missing: {layer_root}"
        assert any(_python_files(layer_root)), f"Clean layer has no Python modules: {layer_root}"
        for path in _python_files(layer_root):
            for line, module, names in _imports(path):
                candidates = (module, *(f"{module}.{name}" for name in names))
                if any(_matches_prefix(candidate, forbidden) for candidate in candidates):
                    rel = path.relative_to(PROJECT_ROOT).as_posix()
                    violations.append(f"{rel}:{line} imports {module}")

    assert not violations, "Dependency rule violations:\n" + "\n".join(violations)


def test_legacy_routes_do_not_increase_direct_database_import_debt() -> None:
    """The legacy baseline may shrink, but new direct DB route imports are blocked."""
    violating_files: set[str] = set()
    for path in _python_files(APP_ROOT / "web" / "routes"):
        if any(
            module == "app.storage.db" and bool(names & _DB_ENTRYPOINTS)
            for _line, module, names in _imports(path)
        ):
            violating_files.add(path.relative_to(PROJECT_ROOT).as_posix())

    unexpected = violating_files - ROUTE_DIRECT_DB_IMPORT_BASELINE
    assert not unexpected, (
        "A new route acquired a direct database import. Move its SQL behind "
        "an application port/adapter. New violations:\n"
        + "\n".join(sorted(unexpected))
    )


def _registered_app():
    # Deliberately lazy: collection of unrelated tests must not pay the current
    # ~11 s web import cost merely because this module was discovered.
    from app.web.main import app  # noqa: PLC0415

    return app


def test_fastapi_method_path_pairs_are_unique() -> None:
    """FastAPI's first-match behaviour must never shadow another handler."""
    app = _registered_app()
    handlers: dict[tuple[str, str], list[str]] = defaultdict(list)
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        endpoint = route.endpoint
        identity = (
            f"{getattr(endpoint, '__module__', '<unknown>')}."
            f"{getattr(endpoint, '__name__', route.name)}"
        )
        for method in route.methods:
            handlers[(method, route.path)].append(identity)

    duplicates = {
        f"{method} {path}": endpoints
        for (method, path), endpoints in handlers.items()
        if len(endpoints) > 1
    }
    assert not duplicates, "Duplicate FastAPI routes:\n" + json.dumps(
        duplicates, indent=2, ensure_ascii=False
    )


def test_collision_fixes_keep_the_intended_handlers_reachable() -> None:
    app = _registered_app()
    get_handlers = {
        route.path: route.endpoint
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in route.methods
    }

    expected = {
        "/features": "app.web.routes.landing",
        "/feature-index": "app.web.routes.feature_index",
        "/changelog": "app.web.routes.changelog",
        "/roadmap/releases": "app.web.routes.landing",
        "/help/shortcuts": "app.web.routes.shortcuts_help",
        "/help/legacy-shortcuts": "app.web.routes.help",
    }
    missing_or_wrong = {
        path: getattr(get_handlers.get(path), "__module__", None)
        for path, module in expected.items()
        if getattr(get_handlers.get(path), "__module__", None) != module
    }
    assert not missing_or_wrong, f"Canonical route handlers changed: {missing_or_wrong}"


def test_registered_route_count_stays_within_budget() -> None:
    count = len(_registered_app().routes)
    assert count <= REGISTERED_ROUTE_BUDGET, (
        f"Registered route budget exceeded ({count} > {REGISTERED_ROUTE_BUDGET}). "
        "Consolidate an existing surface or explicitly review and ratchet the budget."
    )


def test_cold_web_import_stays_within_regression_budget() -> None:
    """Measure a real fresh interpreter, not an already-warm pytest import."""
    marker = "PERSONA_STARTUP_PROBE:"
    script = (
        "import json,time;"
        "started=time.perf_counter();"
        "import app.web.main as main;"
        "elapsed=time.perf_counter()-started;"
        f"print('{marker}'+json.dumps({{'seconds':elapsed,'routes':len(main.app.routes)}}))"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=COLD_IMPORT_BUDGET_SECONDS + 15,
        check=False,
    )
    assert completed.returncode == 0, (
        "Cold import process failed:\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    probe_line = next(
        (line for line in completed.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    assert probe_line is not None, f"Startup probe produced no result:\n{completed.stdout}"
    result = json.loads(probe_line.removeprefix(marker))
    assert result["seconds"] <= COLD_IMPORT_BUDGET_SECONDS, (
        "Cold app.web.main import exceeded budget "
        f"({result['seconds']:.2f}s > {COLD_IMPORT_BUDGET_SECONDS:.2f}s)."
    )
    assert result["routes"] <= REGISTERED_ROUTE_BUDGET
