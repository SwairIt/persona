"""Full-app feature index — meta-documentation of every UI route.

Walks the live :class:`fastapi.FastAPI` route table and pairs each
user-facing path with a curated one-line description plus a category.
Powers the ``/features`` discovery page.

The index is *intentionally* curated rather than fully auto-generated:
- only ``GET`` routes that render a page (or top-level JSON endpoints
  users actually hit) are interesting,
- ``/static/*``, ``/docs``, ``/redoc``, ``/openapi.json``, and any path
  containing a path parameter (``{...}``) are filtered out because they
  are not browsable,
- the ``hint`` column is hand-picked per route so the listing reads as
  a human catalogue rather than a regex dump of route docstrings.

Anything in the running app that isn't in :data:`_ROUTE_METADATA` falls
back to a tag-derived hint so newly added routes still show up, just
without prose. See :func:`build_feature_index`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

from fastapi.routing import APIRoute

from app.logging_setup import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

log = get_logger("persona.feature_index")

Category = Literal[
    "timeline",
    "search",
    "capture",
    "ocr",
    "llm",
    "export",
    "share",
    "admin",
    "stats",
    "settings",
    "integrations",
]

CATEGORY_ORDER: tuple[Category, ...] = (
    "timeline",
    "search",
    "capture",
    "ocr",
    "llm",
    "export",
    "share",
    "stats",
    "settings",
    "integrations",
    "admin",
)

CATEGORY_LABELS: dict[Category, str] = {
    "timeline": "Timeline & browsing",
    "search": "Search & discovery",
    "capture": "Capture & ingestion",
    "ocr": "OCR & text",
    "llm": "AI & summaries",
    "export": "Export",
    "share": "Sharing",
    "admin": "Admin & maintenance",
    "stats": "Stats & analytics",
    "settings": "Settings",
    "integrations": "Integrations",
}


class FeatureEntry(TypedDict):
    """One row in the feature index."""

    path: str
    title: str
    hint: str
    category: Category


# Manually curated metadata. Keys are the exact path strings registered
# on the FastAPI app. ``title`` is the short label shown on the card;
# ``hint`` is the one-liner; ``category`` slots the card into a section.
_ROUTE_METADATA: dict[str, tuple[str, str, Category]] = {
    "/": ("Timeline", "Newest captures grouped by hour", "timeline"),
    "/welcome": ("Welcome", "First-run landing page", "timeline"),
    "/calendar": ("Calendar", "Month grid of capture counts", "timeline"),
    "/heatmap": ("Heatmap", "GitHub-style activity heatmap", "stats"),
    "/range": ("Range timeline", "Pick a custom date range to browse", "timeline"),
    "/day-kanban": ("Day kanban", "Today's captures as a kanban board", "timeline"),
    "/day-scrubber": ("Day scrubber", "Drag-the-handle review of a single day", "timeline"),
    "/notes": ("Notes (all)", "Every note across every screenshot", "timeline"),
    "/notes/timeline": ("Notes timeline", "Notes laid out chronologically", "timeline"),
    "/notes/search": ("Notes search", "Full-text search across notes only", "search"),
    "/journal": ("Journal", "Daily markdown journal entries", "timeline"),
    "/favourites": ("Favourites", "Pinned + starred screenshots", "timeline"),
    "/inbox": ("Inbox", "Unreviewed captures awaiting triage", "timeline"),
    "/pin": ("Pinned", "Captures you manually pinned to keep", "timeline"),
    "/recycle": ("Recycle bin", "Soft-deleted captures, restorable", "timeline"),
    "/apps": ("Apps index", "Per-application capture rollups", "stats"),
    "/topics": ("Topics", "Auto-clustered topic buckets", "search"),
    "/collections": ("Collections", "Saved rule-based collections", "search"),
    "/keywords": ("Keywords", "Most frequent OCR keywords", "search"),
    "/tags": ("Tags", "Tag cloud and tag pages", "search"),
    "/saved-searches": ("Saved searches", "Stored search queries", "search"),
    "/search": ("Search", "Hybrid keyword + semantic search", "search"),
    "/search/facets": ("Search facets", "Filter search by app, tag, date", "search"),
    "/palette": ("Command palette", "Cmd+K palette data feed", "search"),
    "/ask": ("Ask", "Natural-language Q&A over your captures", "llm"),
    "/qa": ("Q&A", "Question-answering UI on your data", "llm"),
    "/summary": ("Summary", "Daily AI summary of your activity", "llm"),
    "/digest": ("Digest", "Daily digests landing page", "llm"),
    "/digest/weekly": ("Weekly digest", "Auto-generated weekly recap", "llm"),
    "/digests/daily": ("Daily digests", "Browse archive of daily digests", "llm"),
    "/digests/weekly": ("Weekly digests", "Browse archive of weekly digests", "llm"),
    "/day-tldr": ("Day TL;DR", "AI one-paragraph summary of a day", "llm"),
    "/note-assist": ("Note assist", "LLM-suggested note text per shot", "llm"),
    "/note-templates": ("Note templates", "Reusable note skeletons", "llm"),
    "/auto-tag": ("Auto-tag", "Suggested tags from OCR content", "llm"),
    "/auto-collections": ("Auto-collections", "Rule-discovered collections", "search"),
    "/focus": ("Focus mode", "Distraction-free single-app view", "timeline"),
    "/reminders": ("Reminders", "Time-based reminder list", "timeline"),
    "/reading": ("Reading", "Saved articles + reading time", "timeline"),
    "/reading-time": ("Reading time", "Estimated minutes per screenshot", "stats"),
    "/stats": ("Stats", "Headline charts and counters", "stats"),
    "/stats.csv": ("Stats CSV", "Downloadable stats spreadsheet", "stats"),
    "/streak": ("Streak", "Daily-capture streak tracker", "stats"),
    "/hours": ("Hour histogram", "Captures binned by hour of day", "stats"),
    "/idle-stats": ("Idle stats", "Idle-vs-active time breakdown", "stats"),
    "/timesheet": ("Timesheet", "App usage as a printable timesheet", "stats"),
    "/tag-trends": ("Tag trends", "How tag usage evolves over time", "stats"),
    "/time-on-app": ("Time on app", "Minutes spent per application", "stats"),
    "/storage-report": ("Storage report", "Disk usage by tier and folder", "stats"),
    "/health": ("Health", "Liveness probe (JSON)", "admin"),
    "/health-dashboard": ("Health dashboard", "All worker / DB health at a glance", "admin"),
    "/doctor": ("Doctor", "Diagnostic checks for common issues", "admin"),
    "/audit": ("Audit log", "Append-only log of admin actions", "admin"),
    "/audit.rss": ("Audit RSS", "Audit log as an RSS feed", "admin"),
    "/about": ("About", "Build info + feature toggle dashboard", "admin"),
    "/help": ("Help", "Keyboard shortcuts + quick tips", "admin"),
    "/features": ("Features", "This page — every route in the app", "admin"),
    "/admin/bulk-delete": ("Bulk delete", "Range/app-scoped deletion tool", "admin"),
    "/process-remap": ("Process remap", "Rename mis-detected process names", "admin"),
    "/redaction": ("Redaction", "Patterns to redact from OCR text", "admin"),
    "/regex-rules": ("Regex rules", "User regex rules for tagging", "admin"),
    "/quiet-hours": ("Quiet hours", "Schedule when capture is paused", "settings"),
    "/whitelist": ("Whitelist", "Apps excluded from capture", "settings"),
    "/app-overrides": ("App overrides", "Per-app capture interval overrides", "settings"),
    "/settings": ("Settings", "Main settings page", "settings"),
    "/settings/api-tokens": ("API tokens", "Manage tokens for the REST API", "settings"),
    "/settings/app-retention": ("App retention", "Per-app data retention windows", "settings"),
    "/settings/backup": ("Settings backup", "Export / restore your settings", "settings"),
    "/settings/smtp": ("SMTP settings", "Configure outbound email", "settings"),
    "/settings/theme": ("Theme", "Light / dark / auto appearance", "settings"),
    "/settings/tag-colour": ("Tag colours", "Pick a colour for every tag", "settings"),
    "/settings/ocr-languages": ("OCR languages", "Languages Tesseract should try", "settings"),
    "/settings/ocr-skip": ("OCR skip", "Apps to skip OCR for", "settings"),
    "/settings/retention-preview": (
        "Retention preview",
        "Preview what retention would delete",
        "settings",
    ),
    "/settings/encrypted-notes": ("Encrypted notes", "Per-note end-to-end encryption", "settings"),
    "/vault": ("Private vault", "Encrypted private screenshots", "settings"),
    "/clipboard": ("Clipboard", "Recent clipboard history", "capture"),
    "/bookmarklet": ("Bookmarklet", "Browser bookmarklet to capture pages", "capture"),
    "/mobile": ("Mobile", "Mobile capture upload endpoint", "capture"),
    "/companion": ("Companion", "Browser-extension companion settings", "integrations"),
    "/webhooks": ("Webhooks", "Outbound webhook subscriptions", "integrations"),
    "/icons": ("Icons", "App-icon library used in the UI", "integrations"),
    "/share": ("Share", "Outbound share links you created", "share"),
    "/share/collection": ("Share collection", "Share a collection by link", "share"),
    "/shot-of-day": ("Shot of the day", "Today's auto-picked highlight", "share"),
    "/shot-of-week": ("Shot of the week", "This week's auto-picked highlight", "share"),
    "/shot-share": ("Shot share", "One-shot public share link", "share"),
    "/public/day": ("Public day", "Public-shareable day page", "share"),
    "/permalinks": ("Permalinks", "Permanent URLs for screenshots", "share"),
    "/diff": ("Diff", "Compare two screenshots side-by-side", "ocr"),
    "/diff/picker": ("Diff picker", "Pick two captures to diff", "ocr"),
    "/diff/slider": ("Diff slider", "Drag-to-compare overlay", "ocr"),
    "/visual-diff": ("Visual diff", "Pixel-level diff of two captures", "ocr"),
    "/dup-suggest": ("Dup suggest", "Likely-duplicate clusters", "ocr"),
    "/ocr": ("OCR status", "OCR queue + worker status", "ocr"),
    "/ocr/admin": ("OCR admin", "OCR retry / requeue admin", "ocr"),
    "/ocr/diff": ("OCR diff", "Compare OCR text between captures", "ocr"),
    "/ocr/languages": ("OCR languages", "Per-language OCR coverage", "ocr"),
    "/ocr/language-stats": ("OCR language stats", "Detected-language histograms", "ocr"),
    "/ocr/near-dup": ("OCR near-duplicates", "Near-duplicate OCR clusters", "ocr"),
    "/ocr/overlay": ("OCR overlay", "Word boxes drawn over the image", "ocr"),
    "/ocr/phrase-tags": ("OCR phrase tags", "Auto-tags from common phrases", "ocr"),
    "/ocr/retry": ("OCR retry", "Re-run OCR on failed captures", "ocr"),
    "/ocr/skip": ("OCR skip", "Apps OCR should ignore", "ocr"),
    "/day-collage": ("Day collage", "Single-image collage of a whole day", "export"),
    "/weekly-pdf": ("Weekly PDF", "Generate a PDF of the past week", "export"),
    "/export": ("Export", "Bulk export landing page", "export"),
    "/export/full": ("Full export", "Export everything as a zip", "export"),
    "/export/journal": ("Journal export", "Export journal as markdown", "export"),
    "/export/ics": ("ICS export", "Export captures to calendar (.ics)", "export"),
    "/export/csv": ("CSV export", "Export captures as CSV", "export"),
    "/export/archive": ("Archive export", "Export the cold archive as zip", "export"),
    "/export/day-collage": ("Day collage export", "Download a day collage image", "export"),
    "/export/pdf": ("PDF export", "Export a date range as PDF", "export"),
    "/export/ocr-txt": ("OCR text export", "Download all OCR text", "export"),
    "/archive": ("Archive", "Cold archive browser", "timeline"),
    "/archive/browse": ("Archive browse", "Paginated archive listing", "timeline"),
    "/archive/search": ("Archive search", "Search inside the cold archive", "search"),
    "/feeds/audit.rss": ("Audit RSS feed", "Admin audit log as RSS", "admin"),
    "/rss": ("RSS", "RSS feed of new captures", "share"),
    "/feeds": ("Feeds", "All RSS / Atom feeds index", "share"),
    "/events": ("Live SSE", "Server-sent live status stream", "integrations"),
}

# Hard-coded supplementary list — top-level pages that have no FastAPI
# route or are surfaced via static / template includes. They are merged
# in unconditionally so the discovery page lists them even if the route
# table doesn't.
_SUPPLEMENTARY: tuple[FeatureEntry, ...] = (
    {
        "path": "/",
        "title": "Timeline",
        "hint": "Newest captures grouped by hour",
        "category": "timeline",
    },
    {
        "path": "/search",
        "title": "Search",
        "hint": "Hybrid keyword + semantic search",
        "category": "search",
    },
    {
        "path": "/ask",
        "title": "Ask",
        "hint": "Natural-language Q&A over your captures",
        "category": "llm",
    },
    {
        "path": "/about",
        "title": "About",
        "hint": "Build info + feature toggle dashboard",
        "category": "admin",
    },
    {
        "path": "/help",
        "title": "Help",
        "hint": "Keyboard shortcuts + quick tips",
        "category": "admin",
    },
    {
        "path": "/features",
        "title": "Features",
        "hint": "This page — every route in the app",
        "category": "admin",
    },
)

# Path prefixes / exact paths to skip when walking the route table.
_SKIP_PREFIXES: tuple[str, ...] = (
    "/static",
    "/api/",
    "/thumbs/",
    "/thumbnail",
    "/screenshot/",
    "/app-icon/",
    "/icons/",
    "/openapi",
    "/docs",
    "/redoc",
)
_SKIP_EXACT: frozenset[str] = frozenset(
    {
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)


def _is_browsable(path: str, methods: set[str]) -> bool:
    """Return True if the route is something a human would open in a tab."""
    if "GET" not in methods:
        return False
    if "{" in path or "}" in path:
        return False
    if path in _SKIP_EXACT:
        return False
    return not any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


def _fallback_metadata(path: str, tags: list[str | None]) -> tuple[str, str, Category]:
    """Synthesise a title/hint/category for routes not in :data:`_ROUTE_METADATA`."""
    title = path.lstrip("/").replace("/", " · ").replace("-", " ").strip() or "Home"
    title = title[:1].upper() + title[1:]
    tag = next((t for t in tags if isinstance(t, str) and t), None)
    hint = f"Tagged: {tag}" if tag else "Auto-discovered route"
    category: Category = _category_from_tag(tag)
    return title, hint, category


# Substring → category mapping, evaluated in declaration order.
# ``ocr`` is intentionally evaluated before ``share`` etc. so that a
# router tag like ``"ocr-share"`` (hypothetical) lands in the ocr
# bucket rather than share.
_TAG_RULES: tuple[tuple[tuple[str, ...], Category], ...] = (
    (("ocr",), "ocr"),
    (("search", "saved", "facet", "topic", "tag"), "search"),
    (("capture", "clipboard", "bookmarklet", "mobile"), "capture"),
    (("llm", "ask", "digest", "summary", "tldr"), "llm"),
    (("export", "csv", "pdf", "collage", "ics"), "export"),
    (("share", "permalink", "shot", "public", "rss", "feed"), "share"),
    (("stat", "trend", "heatmap", "streak", "histogram"), "stats"),
    (("setting", "vault", "retention", "smtp", "theme"), "settings"),
    (("webhook", "companion", "icon", "integration"), "integrations"),
    (("timeline", "calendar", "kanban", "scrubber", "journal"), "timeline"),
)


def _category_from_tag(tag: str | None) -> Category:
    """Map a FastAPI router tag to one of the public :data:`Category` slots."""
    if not tag:
        return "admin"
    needle = tag.lower()
    for substrings, category in _TAG_RULES:
        if any(s in needle for s in substrings):
            return category
    return "admin"


async def build_feature_index(app: FastAPI) -> list[FeatureEntry]:
    """Build the sorted, de-duplicated list of feature entries.

    Walks ``app.routes`` once, keeping only browsable ``GET`` endpoints,
    pairs each with curated metadata (falling back to a tag-derived
    hint), then merges in :data:`_SUPPLEMENTARY` and sorts by category
    (per :data:`CATEGORY_ORDER`) then by title.
    """
    seen: dict[str, FeatureEntry] = {}

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not _is_browsable(route.path, route.methods):
            continue

        meta = _ROUTE_METADATA.get(route.path)
        if meta is not None:
            title, hint, category = meta
        else:
            tag_strings: list[str | None] = [str(t) if t is not None else None for t in route.tags]
            title, hint, category = _fallback_metadata(route.path, tag_strings)

        seen[route.path] = {
            "path": route.path,
            "title": title,
            "hint": hint,
            "category": category,
        }

    for entry in _SUPPLEMENTARY:
        seen.setdefault(entry["path"], entry)

    order_index = {cat: i for i, cat in enumerate(CATEGORY_ORDER)}
    entries = sorted(
        seen.values(),
        key=lambda e: (order_index.get(e["category"], len(order_index)), e["title"].lower()),
    )

    log.info(
        "feature_index.built",
        total=len(entries),
        by_category={
            cat: sum(1 for e in entries if e["category"] == cat) for cat in CATEGORY_ORDER
        },
    )
    return entries
