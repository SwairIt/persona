"""Share-collection PDF export — bundle every shot from one signed link.

The v0.21 share-collection feature (table ``share_collections``, route
:mod:`app.web.routes.share_collection`) lets the owner stitch N
screenshots behind a single signed URL. This module renders that
same set of shots as a printable PDF:

* page 1 — cover with the collection's title and the
  earliest → latest ``captured_at`` from its screenshots.
* one page per shot — thumbnail (if available) + caption
  (``HH:MM · app — window``) + first ``_OCR_PREVIEW_LIMIT`` chars of
  the cached OCR text.

The ``reportlab`` dependency is intentionally optional — Persona has
shipped a "feature degrades, install ``reportlab`` to enable" contract
for every PDF surface (:mod:`app.pdf_export`, :mod:`app.weekly_pdf`).
We honour that here too: a missing dep returns ``status="missing_dep"``
and the HTTP route in :mod:`app.web.routes.share_collection_pdf`
short-circuits to an explanatory banner instead of a 500.

Heavy work — PIL ``ImageReader`` and the reportlab ``doc.build`` —
runs inside :func:`anyio.to_thread.run_sync` so the FastAPI event loop
keeps serving requests while a slow A4 page is being typeset.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import anyio.to_thread

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

log = get_logger("persona.share_collection_pdf")

# Mirrors :mod:`app.pdf_export` so the look-and-feel is consistent
# across Persona's three PDF surfaces (day, week, share-collection).
_OCR_PREVIEW_LIMIT = 300
_CAPTION_LIMIT = 110


class CollectionPdfResult(TypedDict):
    """Return payload for :func:`build_collection_pdf`.

    ``status`` is one of:

    * ``"missing_dep"`` — ``reportlab`` is not installed; ``path`` /
      ``size_bytes`` / ``pages`` are zero.
    * ``"not_found"`` — no row matches ``slug`` in ``share_collections``.
    * ``"expired"`` — the collection's ``expires_unix`` has passed.
    * ``"corrupt"`` — the stored ``screenshot_ids`` JSON failed to parse.
    * ``"empty"`` — the row exists but resolves to zero live screenshots
      (every id was hard-deleted after the link was minted).
    * ``"ok"`` — the PDF was written; ``path`` and ``size_bytes`` are valid.
    """

    status: str
    path: str | None
    size_bytes: int
    pages: int


class _CollectionData(TypedDict):
    """Internal view of one share-collection ready for PDF rendering."""

    slug: str
    title: str | None
    shots: list[dict[str, Any]]
    earliest: datetime | None
    latest: datetime | None


def _truncate(text: str, limit: int) -> str:
    """Flatten whitespace and cap to ``limit`` characters with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _escape(text: str) -> str:
    """Minimal HTML-escape for reportlab's ``Paragraph`` markup."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_caption(shot: dict[str, Any]) -> str:
    """Build a one-line ``HH:MM · app — window`` caption.

    Falls back gracefully when individual fields are NULL so the body
    page header is never empty — even a bare ``HH:MM · —`` is more
    useful than a blank line.
    """
    captured = shot.get("captured_at")
    ts = captured.strftime("%H:%M") if isinstance(captured, datetime) else "??:??"
    app_name = shot.get("app_name") or "—"
    title = shot.get("window_title") or ""
    head = f"{ts} · {app_name}"
    if title:
        head = f"{head} — {title}"
    return _truncate(head, _CAPTION_LIMIT)


def _resolve_thumbnail(raw: str | None) -> Path | None:
    """Return a usable filesystem path for the stored ``thumbnail_path``.

    Persona historically stored both absolute and relative paths, so try
    the raw value first and only fall back to the project root if it is
    missing — same contract as :func:`app.pdf_export._resolve_thumbnail`.
    """
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_file():
        return candidate
    if not candidate.is_absolute():
        rooted = Path.cwd() / candidate
        if rooted.is_file():
            return rooted
    return None


async def _load_collection(slug: str) -> tuple[str, _CollectionData | None]:
    """Resolve ``slug`` against ``share_collections`` + ``screenshots``.

    Returns ``(status, data)`` so the caller can branch without having
    to re-check status codes:

    * ``("not_found", None)`` — no row.
    * ``("expired", None)`` — row exists but past ``expires_unix``.
    * ``("corrupt", None)`` — ``screenshot_ids`` JSON failed to parse.
    * ``("ok", data)`` — possibly with an empty ``shots`` list when every
      referenced id has been hard-deleted. The HTTP route maps that to
      ``"empty"`` so the cover page never tries to render an empty PDF.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT token, title, screenshot_ids, expires_unix "
            "FROM share_collections WHERE token = ?",
            (slug,),
        )
        row = await cursor.fetchone()
        if row is None:
            return "not_found", None

        expires_unix = int(row["expires_unix"])
        if expires_unix < int(time.time()):
            return "expired", None

        try:
            ids_raw = json.loads(row["screenshot_ids"])
        except (TypeError, ValueError) as exc:
            log.error(
                "share_collection_pdf.corrupt_ids",
                slug=slug,
                error=str(exc),
            )
            return "corrupt", None

        # Cast defensively — ``json.loads`` could yield any JSON value
        # if the table was hand-edited. We only accept a list of ints.
        if not isinstance(ids_raw, list):
            log.error("share_collection_pdf.corrupt_ids", slug=slug, kind=type(ids_raw).__name__)
            return "corrupt", None

        shots: list[dict[str, Any]] = []
        for sid_raw in ids_raw:
            try:
                sid = int(sid_raw)
            except (TypeError, ValueError):
                continue
            shot = await get_screenshot(conn, sid)
            if shot is None:
                # v0.14 keeps ``share_collections`` FK-free on purpose;
                # a hard-deleted screenshot simply drops out of the PDF.
                continue
            shots.append(
                {
                    "id": shot.id,
                    "captured_at": shot.captured_at,
                    "app_name": shot.app_name,
                    "window_title": shot.window_title,
                    "thumbnail_path": shot.thumbnail_path,
                    "ocr_text": shot.ocr_text,
                }
            )

    earliest: datetime | None = None
    latest: datetime | None = None
    for shot_row in shots:
        captured = shot_row["captured_at"]
        if not isinstance(captured, datetime):
            continue
        if earliest is None or captured < earliest:
            earliest = captured
        if latest is None or captured > latest:
            latest = captured

    data: _CollectionData = {
        "slug": slug,
        "title": (str(row["title"]) if row["title"] is not None else None),
        "shots": shots,
        "earliest": earliest,
        "latest": latest,
    }
    return "ok", data


def _format_date_range(
    earliest: datetime | None, latest: datetime | None
) -> str:
    """Render ``earliest → latest`` for the cover page.

    Same-day collections collapse to a single date so the cover never
    reads ``2026-06-01 → 2026-06-01``.
    """
    if earliest is None and latest is None:
        return "(no captured timestamps)"
    if earliest is None or latest is None:
        only = earliest or latest
        assert only is not None
        return only.strftime("%Y-%m-%d %H:%M UTC")
    if earliest.date() == latest.date():
        return earliest.strftime("%Y-%m-%d")
    return f"{earliest.strftime('%Y-%m-%d')} → {latest.strftime('%Y-%m-%d')}"


def _build_pdf(  # noqa: PLR0915 - linear reportlab story builder
    data: _CollectionData,
    output_path: Path,
) -> int:
    """Render the share-collection PDF and return the page count.

    Imported lazily so ``reportlab`` only has to exist when an export
    actually runs — matches the contract in :mod:`app.pdf_export`.
    """
    from reportlab.lib import colors  # noqa: PLC0415 - optional dep, lazy
    from reportlab.lib.pagesizes import A4  # noqa: PLC0415
    from reportlab.lib.styles import (  # noqa: PLC0415
        ParagraphStyle,
        getSampleStyleSheet,
    )
    from reportlab.lib.units import cm  # noqa: PLC0415
    from reportlab.lib.utils import ImageReader  # noqa: PLC0415
    from reportlab.platypus import (  # noqa: PLC0415
        Image,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ShareCollectionTitle",
        parent=styles["Title"],
        fontSize=24,
        leading=28,
        textColor=colors.HexColor("#1f2937"),
    )
    h2_style = ParagraphStyle(
        "ShareCollectionH2",
        parent=styles["Heading2"],
        fontSize=14,
        leading=18,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "ShareCollectionBody",
        parent=styles["BodyText"],
        fontSize=10.5,
        leading=14,
    )
    caption_style = ParagraphStyle(
        "ShareCollectionCaption",
        parent=styles["BodyText"],
        fontSize=9.5,
        leading=12,
        textColor=colors.HexColor("#374151"),
    )
    mono_style = ParagraphStyle(
        "ShareCollectionMono",
        parent=styles["Code"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#111827"),
    )

    page_count = 0

    def _on_page(_canvas: Any, _doc: Any) -> None:
        nonlocal page_count
        page_count += 1

    cover_title = data["title"] or f"Shared collection ({len(data['shots'])} screenshots)"
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"Persona share collection — {cover_title}",
        author="Persona",
    )
    page_width, _page_height = A4
    usable_width = page_width - doc.leftMargin - doc.rightMargin

    story: list[Any] = []

    # ---- Cover page --------------------------------------------------------
    story.append(Paragraph(_escape(cover_title), title_style))
    story.append(Spacer(1, 0.4 * cm))
    story.append(
        Paragraph(
            f"Share-collection PDF generated "
            f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
            caption_style,
        )
    )
    story.append(Spacer(1, 0.8 * cm))

    story.append(Paragraph("Date range", h2_style))
    story.append(
        Paragraph(
            _escape(_format_date_range(data["earliest"], data["latest"])),
            body_style,
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Totals", h2_style))
    story.append(
        Paragraph(
            f"Screenshots in this collection: <b>{len(data['shots'])}</b>",
            body_style,
        )
    )

    # ---- One body page per shot --------------------------------------------
    for shot in data["shots"]:
        story.append(PageBreak())
        story.append(Paragraph(_escape(_format_caption(shot)), h2_style))
        story.append(Spacer(1, 0.3 * cm))

        thumb = _resolve_thumbnail(shot.get("thumbnail_path"))
        if thumb is not None:
            try:
                reader = ImageReader(str(thumb))
                img_w, img_h = reader.getSize()
            except Exception as exc:  # pragma: no cover - PIL/IO surprises
                log.warning(
                    "share_collection_pdf.thumbnail_unreadable",
                    path=str(thumb),
                    error=str(exc),
                )
                story.append(
                    Paragraph("<i>(thumbnail unreadable)</i>", caption_style)
                )
                story.append(Spacer(1, 0.3 * cm))
            else:
                ratio = img_h / img_w if img_w else 1.0
                draw_w = min(usable_width, 16 * cm)
                draw_h = draw_w * ratio
                max_h = 14 * cm
                if draw_h > max_h:
                    draw_h = max_h
                    draw_w = draw_h / ratio if ratio else draw_w
                story.append(Image(str(thumb), width=draw_w, height=draw_h))
                story.append(Spacer(1, 0.3 * cm))
        else:
            story.append(
                Paragraph("<i>(thumbnail unavailable)</i>", caption_style)
            )
            story.append(Spacer(1, 0.3 * cm))

        ocr_text = shot.get("ocr_text") or ""
        if isinstance(ocr_text, str) and ocr_text.strip():
            preview = _truncate(ocr_text, _OCR_PREVIEW_LIMIT)
            story.append(Paragraph("OCR preview", h2_style))
            story.append(Paragraph(_escape(preview), mono_style))
        else:
            story.append(Paragraph("<i>(no OCR text)</i>", caption_style))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return page_count


async def build_collection_pdf(
    slug: str,
    output_path: Path | str,
) -> CollectionPdfResult:
    """Render a PDF bundle for the share-collection identified by ``slug``.

    ``slug`` is the same token the v0.21 share route accepts at
    ``/share/collection/{token}``. The function returns a structured
    dict (see :class:`CollectionPdfResult`) instead of raising so HTTP
    and CLI callers can branch on ``status`` without try/except.
    """
    try:
        import reportlab  # noqa: F401, PLC0415 - probe for optional dep
    except ImportError:
        log.info("share_collection_pdf.missing_dep", slug=slug)
        return CollectionPdfResult(
            status="missing_dep",
            path=None,
            size_bytes=0,
            pages=0,
        )

    status, data = await _load_collection(slug)
    if status != "ok" or data is None:
        log.info("share_collection_pdf.skip", slug=slug, status=status)
        return CollectionPdfResult(
            status=status,
            path=None,
            size_bytes=0,
            pages=0,
        )

    if not data["shots"]:
        log.info("share_collection_pdf.empty", slug=slug)
        return CollectionPdfResult(
            status="empty",
            path=None,
            size_bytes=0,
            pages=0,
        )

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # PIL ``ImageReader`` + reportlab ``doc.build`` are blocking. Punt
    # both into a thread so the FastAPI event loop keeps serving while
    # a 50-shot collection is being typeset.
    pages = await anyio.to_thread.run_sync(_build_pdf, data, out_path)
    size_bytes = out_path.stat().st_size if out_path.exists() else 0

    log.info(
        "share_collection_pdf.built",
        slug=slug,
        path=str(out_path),
        pages=pages,
        shots=len(data["shots"]),
        size_bytes=size_bytes,
    )

    return CollectionPdfResult(
        status="ok",
        path=str(out_path),
        size_bytes=size_bytes,
        pages=pages,
    )


__all__ = ["CollectionPdfResult", "build_collection_pdf"]
