"""Per-day PDF export — title page, one page per shot, day notes.

Stitches together everything Persona knows about a single calendar day into
one printable PDF: a cover page with totals, one body page per screenshot
(thumbnail + caption + first 300 chars of OCR), and final pages listing the
day's free-text notes in chronological order.

The ``reportlab`` dependency is intentionally optional. When it is not
installed the function short-circuits with ``status="missing_dep"`` so the
caller (web route or CLI) can render a friendly banner instead of crashing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso, parse_iso

log = get_logger("persona.pdf")

_OCR_PREVIEW_LIMIT = 300
_CAPTION_LIMIT = 110


class PdfExportResult(TypedDict):
    """Return payload for :func:`export_day_pdf`."""

    status: str
    path: str | None
    pages: int
    screenshots: int
    notes: int
    size_bytes: int


def _parse_day_iso(day_iso: str) -> date:
    """Parse ``YYYY-MM-DD`` into a :class:`date` or raise :class:`ValueError`."""
    return datetime.strptime(day_iso, "%Y-%m-%d").date()


def _truncate(text: str, limit: int) -> str:
    """Flatten whitespace and cap to ``limit`` characters with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


class _DayData(TypedDict):
    shots: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    top_apps: list[tuple[str, int]]


async def _load_day_data(target: date) -> _DayData:
    """Pull screenshots + notes + top apps for ``target`` (UTC day boundaries)."""
    start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, captured_at, app_name, window_title, thumbnail_path, ocr_text
            FROM screenshots
            WHERE captured_at >= ? AND captured_at < ?
            ORDER BY captured_at ASC
            """,
            (iso(start), iso(end)),
        )
        shots = [
            {
                "id": int(row["id"]),
                "captured_at": parse_iso(str(row["captured_at"])),
                "app_name": row["app_name"],
                "window_title": row["window_title"],
                "thumbnail_path": row["thumbnail_path"],
                "ocr_text": row["ocr_text"],
            }
            for row in await cursor.fetchall()
        ]

        cursor = await conn.execute(
            """
            SELECT n.screenshot_id, n.body, n.updated_at,
                   s.app_name, s.window_title
            FROM screenshot_notes n
            JOIN screenshots s ON s.id = n.screenshot_id
            WHERE s.captured_at >= ? AND s.captured_at < ?
            ORDER BY n.updated_at ASC
            """,
            (iso(start), iso(end)),
        )
        notes = [
            {
                "screenshot_id": int(row["screenshot_id"]),
                "body": str(row["body"]),
                "updated_at": parse_iso(str(row["updated_at"])),
                "app_name": row["app_name"],
                "window_title": row["window_title"],
            }
            for row in await cursor.fetchall()
        ]

        cursor = await conn.execute(
            """
            SELECT app_name, COUNT(*) AS n FROM screenshots
            WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL
            GROUP BY app_name ORDER BY n DESC LIMIT 8
            """,
            (iso(start), iso(end)),
        )
        top_apps = [
            (str(row["app_name"]), int(row["n"])) for row in await cursor.fetchall()
        ]

    return {"shots": shots, "notes": notes, "top_apps": top_apps}


def _format_caption(shot: dict[str, Any]) -> str:
    """Build a one-line caption ``HH:MM - app - window`` for the body page."""
    ts = shot["captured_at"].strftime("%H:%M")
    app_name = shot["app_name"] or "—"
    title = shot["window_title"] or ""
    head = f"{ts} · {app_name}"
    if title:
        head = f"{head} — {title}"
    return _truncate(head, _CAPTION_LIMIT)


def _build_pdf(  # noqa: PLR0915, PLR0912 - linear reportlab story builder
    target: date,
    data: _DayData,
    output_path: Path,
) -> int:
    """Render the PDF to ``output_path`` and return the page count.

    Imported here so the ``reportlab`` dependency is only required when an
    export actually runs.
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
        "PersonaTitle",
        parent=styles["Title"],
        fontSize=24,
        leading=28,
        textColor=colors.HexColor("#1f2937"),
    )
    h2_style = ParagraphStyle(
        "PersonaH2",
        parent=styles["Heading2"],
        fontSize=14,
        leading=18,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "PersonaBody",
        parent=styles["BodyText"],
        fontSize=10.5,
        leading=14,
    )
    caption_style = ParagraphStyle(
        "PersonaCaption",
        parent=styles["BodyText"],
        fontSize=9.5,
        leading=12,
        textColor=colors.HexColor("#374151"),
    )
    mono_style = ParagraphStyle(
        "PersonaMono",
        parent=styles["Code"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#111827"),
    )

    page_count = 0

    def _on_page(_canvas: Any, _doc: Any) -> None:
        nonlocal page_count
        page_count += 1

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"Persona day {target.isoformat()}",
        author="Persona",
    )
    page_width, _page_height = A4
    usable_width = page_width - doc.leftMargin - doc.rightMargin

    story: list[Any] = []

    # ---- Page 1: title + totals ----------------------------------------
    story.append(Paragraph(f"Persona — {target.isoformat()}", title_style))
    story.append(Spacer(1, 0.4 * cm))
    story.append(
        Paragraph(
            f"Day report generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
            caption_style,
        )
    )
    story.append(Spacer(1, 0.8 * cm))

    story.append(Paragraph("Totals", h2_style))
    story.append(
        Paragraph(f"Screenshots: <b>{len(data['shots'])}</b>", body_style)
    )
    story.append(Paragraph(f"Notes: <b>{len(data['notes'])}</b>", body_style))
    story.append(Spacer(1, 0.6 * cm))

    if data["top_apps"]:
        story.append(Paragraph("Top apps", h2_style))
        for app_name, count in data["top_apps"]:
            story.append(
                Paragraph(f"• <b>{_escape(app_name)}</b> — {count} captures", body_style)
            )

    # ---- Body pages: one per screenshot --------------------------------
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
                log.warning("pdf.thumbnail_unreadable", path=str(thumb), error=str(exc))
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
        if ocr_text.strip():
            preview = _truncate(ocr_text, _OCR_PREVIEW_LIMIT)
            story.append(Paragraph("OCR preview", h2_style))
            story.append(Paragraph(_escape(preview), mono_style))
        else:
            story.append(Paragraph("<i>(no OCR text)</i>", caption_style))

    # ---- Notes page(s): chronological ----------------------------------
    if data["notes"]:
        story.append(PageBreak())
        story.append(Paragraph(f"Notes — {target.isoformat()}", title_style))
        story.append(Spacer(1, 0.5 * cm))
        for note in data["notes"]:
            ts = note["updated_at"].strftime("%H:%M")
            heading = note["app_name"] or "—"
            if note["window_title"]:
                heading = f"{heading} — {note['window_title']}"
            story.append(
                Paragraph(
                    f"{ts} · {_escape(heading)} "
                    f"<font color='#6b7280'>#{note['screenshot_id']}</font>",
                    h2_style,
                )
            )
            story.append(Paragraph(_escape(note["body"]).replace("\n", "<br/>"), body_style))
            story.append(Spacer(1, 0.3 * cm))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return page_count


def _escape(text: str) -> str:
    """Minimal HTML-escape for reportlab's ``Paragraph`` markup."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _resolve_thumbnail(raw: str | None) -> Path | None:
    """Return a usable filesystem path for the stored ``thumbnail_path``.

    Persona historically stored both absolute and relative paths, so try the
    raw value first and only fall back to the project root if it is missing.
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


async def export_day_pdf(day_iso: str, output_path: Path | str) -> PdfExportResult:
    """Render a multi-page PDF for ``day_iso`` (``YYYY-MM-DD``).

    The function returns a structured dict — see :class:`PdfExportResult` —
    rather than raising, so HTTP and CLI callers can branch on ``status``:

    * ``"missing_dep"`` — ``reportlab`` is not installed.
    * ``"empty"`` — no screenshots for the requested day.
    * ``"ok"`` — PDF was written; ``path`` and ``size_bytes`` are valid.
    * ``"bad_date"`` — ``day_iso`` does not match ``YYYY-MM-DD``.
    """
    try:
        target = _parse_day_iso(day_iso)
    except ValueError:
        log.warning("pdf.bad_date", day=day_iso)
        return PdfExportResult(
            status="bad_date",
            path=None,
            pages=0,
            screenshots=0,
            notes=0,
            size_bytes=0,
        )

    try:
        import reportlab  # noqa: F401, PLC0415 - probe for optional dep
    except ImportError:
        log.info("pdf.missing_dep", day=target.isoformat())
        return PdfExportResult(
            status="missing_dep",
            path=None,
            pages=0,
            screenshots=0,
            notes=0,
            size_bytes=0,
        )

    data = await _load_day_data(target)
    if not data["shots"] and not data["notes"]:
        log.info("pdf.empty", day=target.isoformat())
        return PdfExportResult(
            status="empty",
            path=None,
            pages=0,
            screenshots=0,
            notes=0,
            size_bytes=0,
        )

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pages = _build_pdf(target, data, out_path)
    size_bytes = out_path.stat().st_size if out_path.exists() else 0

    log.info(
        "pdf.built",
        day=target.isoformat(),
        path=str(out_path),
        pages=pages,
        screenshots=len(data["shots"]),
        notes=len(data["notes"]),
        size_bytes=size_bytes,
    )

    return PdfExportResult(
        status="ok",
        path=str(out_path),
        pages=pages,
        screenshots=len(data["shots"]),
        notes=len(data["notes"]),
        size_bytes=size_bytes,
    )


__all__ = ["PdfExportResult", "export_day_pdf"]
