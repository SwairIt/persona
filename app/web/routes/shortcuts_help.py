"""Keyboard shortcuts cheatsheet — overlay + dedicated page.

Adds a single source-of-truth catalogue of every keyboard shortcut wired
up across :file:`keyboard_shortcuts.js`, :file:`search_keyboard.js`,
:file:`quick_pin.js`, :file:`image_viewer.js`, and the
:mod:`app.web.routes.theme` toggle. Users press ``?`` from any page and a
modal overlay (built client-side by :file:`shortcuts_overlay.js`) fetches
the JSON endpoint below to render the list.

The dedicated :http:get:`/help/shortcuts` page is the same data rendered
server-side for users who want a permanent URL (bookmarkable, printable,
indexable by the in-app search). Both surfaces share the
:data:`SHORTCUTS` list below — keep edits in one place.

Companion files:

* :file:`app/web/templates/shortcuts_help.html` — Tailwind table per
  category for the dedicated page.
* :file:`app/web/static/shortcuts_overlay.js` — global ``?`` listener +
  in-page modal that consumes the JSON endpoint.

Routes wired here:

* ``GET /help/shortcuts``           — HTML cheatsheet page.
* ``GET /api/help/shortcuts.json``  — same list as JSON for the overlay
  and any external consumers.
"""

from __future__ import annotations

from typing import Final, TypedDict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["help", "shortcuts"])

_log = get_logger("persona.shortcuts_help")


class Shortcut(TypedDict):
    """One row in the cheatsheet — category + binding + bilingual label.

    ``description_en`` and ``description_ru`` are both supplied so the
    JSON endpoint stays useful for external consumers (browser
    extensions, docs site) that may not know the operator's UI locale.
    The HTML page picks the locale-appropriate string at render time.
    """

    category: str
    key_combo: str
    description_en: str
    description_ru: str


# Hardcoded source of truth. The order inside the list is the order the
# rows render — categories are grouped contiguously so we never need a
# stable-sort pass at render time. Edits here are picked up by both
# surfaces (the HTML page and the JSON the overlay fetches) without any
# further wiring.
SHORTCUTS: Final[list[Shortcut]] = [
    # Navigation — "g" prefix sequences jump straight to a section.
    {
        "category": "Navigation",
        "key_combo": "g t",
        "description_en": "Go to Timeline",
        "description_ru": "Лента",
    },
    {
        "category": "Navigation",
        "key_combo": "g s",
        "description_en": "Go to Search",
        "description_ru": "Поиск",
    },
    {
        "category": "Navigation",
        "key_combo": "g a",
        "description_en": "Go to Ask",
        "description_ru": "Ask",
    },
    {
        "category": "Navigation",
        "key_combo": "g m",
        "description_en": "Go to Memory",
        "description_ru": "Память",
    },
    {
        "category": "Navigation",
        "key_combo": "g c",
        "description_en": "Go to Capture settings",
        "description_ru": "Захват (settings/capture)",
    },
    {
        "category": "Navigation",
        "key_combo": "g k",
        "description_en": "Open command palette",
        "description_ru": "Команды (palette)",
    },
    # Capture — frame, pause, microphone.
    {
        "category": "Capture",
        "key_combo": "c",
        "description_en": "Capture a frame now",
        "description_ru": "Снять кадр сейчас",
    },
    {
        "category": "Capture",
        "key_combo": "p",
        "description_en": "Pause / Resume capture",
        "description_ru": "Пауза/Старт",
    },
    {
        "category": "Capture",
        "key_combo": "m",
        "description_en": "Toggle microphone",
        "description_ru": "Микрофон toggle",
    },
    # Search — focus + dismiss.
    {
        "category": "Search",
        "key_combo": "/",
        "description_en": "Focus the search input",
        "description_ru": "Сфокусировать поиск",
    },
    {
        "category": "Search",
        "key_combo": "Esc",
        "description_en": "Close overlay",
        "description_ru": "Закрыть overlay",
    },
    # UI — meta toggles + this cheatsheet itself.
    {
        "category": "UI",
        "key_combo": "?",
        "description_en": "Show this cheatsheet",
        "description_ru": "Эта справка",
    },
    {
        "category": "UI",
        "key_combo": "t",
        "description_en": "Toggle theme",
        "description_ru": "Сменить тему",
    },
    {
        "category": "UI",
        "key_combo": "Cmd+K",
        "description_en": "Open command palette",
        "description_ru": "Open command palette",
    },
]

# Category render order. Pinned here rather than derived from the
# ``SHORTCUTS`` list so a future reordering of the data list never
# accidentally reorders the UI (e.g. moving a shortcut into a different
# category without touching the visual hierarchy).
CATEGORY_ORDER: Final[tuple[str, ...]] = ("Navigation", "Capture", "Search", "Tags", "UI")


class Section(TypedDict):
    """One rendered category — header + the rows inside it.

    Built by :func:`_group_by_category` and consumed by the Jinja
    template; keeping it typed lets mypy --strict see the ``rows`` list
    is iterable for the structlog row-count below.
    """

    category: str
    rows: list[Shortcut]


def _group_by_category() -> list[Section]:
    """Bucket :data:`SHORTCUTS` into render-ready category sections.

    Returns a list of :class:`Section` dicts in :data:`CATEGORY_ORDER`
    order. Empty buckets (e.g. ``Tags`` today — no shortcuts yet) are
    dropped so the template doesn't render an empty section header.
    """
    buckets: dict[str, list[Shortcut]] = {name: [] for name in CATEGORY_ORDER}
    for row in SHORTCUTS:
        bucket = buckets.get(row["category"])
        if bucket is None:
            _log.warning(
                "shortcuts_help.unknown_category",
                category=row["category"],
                key_combo=row["key_combo"],
            )
            continue
        bucket.append(row)
    sections: list[Section] = []
    for name in CATEGORY_ORDER:
        rows = buckets[name]
        if not rows:
            continue
        sections.append({"category": name, "rows": rows})
    return sections


@router.get("/help/shortcuts", response_class=HTMLResponse)
async def shortcuts_help_page(request: Request) -> HTMLResponse:
    """Render the dedicated cheatsheet page (Tailwind tables per category)."""
    sections = _group_by_category()
    _log.debug(
        "shortcuts_help.render",
        sections=len(sections),
        rows=sum(len(s["rows"]) for s in sections),
    )
    return templates.TemplateResponse(
        request,
        "shortcuts_help.html",
        {
            "title": "Горячие клавиши",
            "active_nav": "settings",
            "sections": sections,
        },
    )


@router.get("/api/help/shortcuts.json", response_class=JSONResponse)
async def shortcuts_help_api(request: Request) -> JSONResponse:
    """Return the cheatsheet as JSON for the overlay + external consumers.

    Shape mirrors :class:`Shortcut`. Consumers should treat the
    ``category`` field as a stable identifier (one of
    :data:`CATEGORY_ORDER`) and the description fields as
    operator-facing text that may be re-translated.
    """
    payload: list[Shortcut] = list(SHORTCUTS)
    return JSONResponse(payload)


__all__ = [
    "CATEGORY_ORDER",
    "SHORTCUTS",
    "Section",
    "Shortcut",
    "router",
]
