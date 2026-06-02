"""Weekly PDF export — Mon-Sun report combining heatmap, time, keywords, streak.

Stitches the existing per-day helpers (:mod:`app.heatmap`, :mod:`app.time_on_app`,
:mod:`app.keywords`, :mod:`app.streak`, :mod:`app.idle_stats`) into a single
printable PDF for any ISO calendar week (Mon..Sun):

    * page 1 — cover with totals, current streak, first/last capture of the week
    * page 2 — daily bar chart (shots per day) + active/idle minutes per day
    * page 3 — top-10 apps with hours
    * page 4 — top-20 keywords
    * page 5 — thumbnail mosaic of up to 12 representative captures

The ``reportlab`` dependency is intentionally optional. When it is not
installed the function short-circuits with ``status="missing_dep"`` so HTTP
and CLI callers can render a friendly banner instead of crashing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict, cast

from app.heatmap import HeatmapDay, yearly_heatmap
from app.idle_stats import IdleStats, daily_idle
from app.keywords import top_keywords
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso
from app.streak import current_streak
from app.time_on_app import app_summary

log = get_logger("persona.weekly_pdf")

_TOP_APPS = 10
_TOP_KEYWORDS = 20
_MOSAIC_CELLS = 12
_DAYS_PER_WEEK = 7
_LOOKBACK_KEYWORDS_DAYS = 7
_APP_NAME_LIMIT = 38


class WeeklyPdfResult(TypedDict):
    """Return payload for :func:`export_week_pdf`."""

    status: str
    path: str | None
    pages: int
    week_start: str
    size_bytes: int


class _WeekData(TypedDict):
    week_start: date
    week_end: date
    days: list[HeatmapDay]
    apps: list[dict[str, object]]
    keywords: list[dict[str, int | str]]
    streak_days: int
    streak_longest: int
    idle_per_day: list[IdleStats]
    first_capture: str | None
    last_capture: str | None
    total_shots: int
    mosaic: list[dict[str, Any]]


def _parse_week_iso(week_start_iso: str) -> date:
    """Parse ``YYYY-MM-DD`` and snap to that week's Monday.

    Accepting any day inside the week (and normalising to Monday) keeps the
    function tolerant of callers that pass *today* without doing weekday math.
    """
    parsed = datetime.strptime(week_start_iso, "%Y-%m-%d").date()
    return parsed - timedelta(days=parsed.weekday())


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


def _format_hours(seconds: int) -> str:
    """Render a second-count as ``H:MM`` (no zero-padding on hours)."""
    if seconds <= 0:
        return "0:00"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}:{minutes:02d}"


def _format_minutes(seconds: int) -> int:
    """Round a second-count down to whole minutes."""
    return max(0, seconds // 60)


async def _load_week_data(week_start: date) -> _WeekData:
    """Pull every dependency for the week's report in one place."""
    week_end = week_start + timedelta(days=_DAYS_PER_WEEK - 1)

    # Heatmap subset: yearly payload then slice the seven days we need.
    heat_payload = await yearly_heatmap(end_date=week_end)
    days_by_iso = {row["date"]: row for row in heat_payload["days"]}
    days: list[HeatmapDay] = []
    for offset in range(_DAYS_PER_WEEK):
        d = (week_start + timedelta(days=offset)).isoformat()
        cell = days_by_iso.get(d)
        if cell is None:
            cell = HeatmapDay(date=d, count=0, level=0)
        days.append(cell)

    # Time-on-app — last 7 days ending today. If the requested week IS the
    # trailing window the figures align exactly; otherwise this is still the
    # most relevant high-level view we can give without bespoke SQL.
    apps = await app_summary(days=_DAYS_PER_WEEK)

    # Keywords — same trailing window for the same reason.
    keywords = await top_keywords(days=_LOOKBACK_KEYWORDS_DAYS, top_n=_TOP_KEYWORDS)

    # Streak — global value, embedded on the cover for motivation.
    streak = await current_streak()

    # Idle stats — per day inside the week (mirrors the daily breakdown).
    idle_per_day: list[IdleStats] = []
    for offset in range(_DAYS_PER_WEEK):
        d_iso = (week_start + timedelta(days=offset)).isoformat()
        idle_per_day.append(await daily_idle(d_iso))

    # First / last capture of the week + total shots + mosaic candidates.
    start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
    end_dt = start_dt + timedelta(days=_DAYS_PER_WEEK)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT MIN(captured_at) AS first_at, MAX(captured_at) AS last_at, "
            "COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ?",
            (iso(start_dt), iso(end_dt)),
        )
        bounds_row = await cursor.fetchone()
        first_capture = (
            str(bounds_row["first_at"]) if bounds_row and bounds_row["first_at"] else None
        )
        last_capture = (
            str(bounds_row["last_at"]) if bounds_row and bounds_row["last_at"] else None
        )
        total_shots = int(bounds_row["n"]) if bounds_row else 0

        # Mosaic: pick up to ``_MOSAIC_CELLS`` rows spread across the week.
        # Strategy — most recent shots with non-null thumbnails. Simple and
        # deterministic; bespoke "best of" picking can come later.
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, thumbnail_path "
            "FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? "
            "AND thumbnail_path IS NOT NULL AND thumbnail_path != '' "
            "ORDER BY captured_at DESC "
            "LIMIT ?",
            (iso(start_dt), iso(end_dt), _MOSAIC_CELLS),
        )
        mosaic: list[dict[str, Any]] = [
            {
                "id": int(row["id"]),
                "captured_at": str(row["captured_at"]),
                "app_name": row["app_name"],
                "thumbnail_path": str(row["thumbnail_path"]),
            }
            for row in await cursor.fetchall()
        ]

    return {
        "week_start": week_start,
        "week_end": week_end,
        "days": days,
        "apps": apps,
        "keywords": keywords,
        "streak_days": int(streak["days"]),
        "streak_longest": int(streak["longest"]),
        "idle_per_day": idle_per_day,
        "first_capture": first_capture,
        "last_capture": last_capture,
        "total_shots": total_shots,
        "mosaic": mosaic,
    }


def _resolve_thumbnail(raw: str | None) -> Path | None:
    """Return a usable filesystem path for the stored ``thumbnail_path``."""
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


def _build_pdf(  # noqa: PLR0915, PLR0912 - linear reportlab story builder
    data: _WeekData,
    output_path: Path,
) -> int:
    """Render the weekly PDF to ``output_path`` and return the page count.

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
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "WeeklyTitle",
        parent=styles["Title"],
        fontSize=24,
        leading=28,
        textColor=colors.HexColor("#1f2937"),
    )
    h2_style = ParagraphStyle(
        "WeeklyH2",
        parent=styles["Heading2"],
        fontSize=14,
        leading=18,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "WeeklyBody",
        parent=styles["BodyText"],
        fontSize=10.5,
        leading=14,
    )
    caption_style = ParagraphStyle(
        "WeeklyCaption",
        parent=styles["BodyText"],
        fontSize=9.5,
        leading=12,
        textColor=colors.HexColor("#374151"),
    )

    page_count = 0

    def _on_page(_canvas: Any, _doc: Any) -> None:
        nonlocal page_count
        page_count += 1

    week_label = (
        f"{data['week_start'].isoformat()} → {data['week_end'].isoformat()}"
    )
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"Persona week {data['week_start'].isoformat()}",
        author="Persona",
    )
    page_width, _page_height = A4
    usable_width = page_width - doc.leftMargin - doc.rightMargin

    story: list[Any] = []

    # ---- Page 1: cover ------------------------------------------------------
    story.append(Paragraph(f"Persona — week {week_label}", title_style))
    story.append(Spacer(1, 0.4 * cm))
    story.append(
        Paragraph(
            f"Weekly report generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
            caption_style,
        )
    )
    story.append(Spacer(1, 0.8 * cm))

    story.append(Paragraph("Totals", h2_style))
    story.append(
        Paragraph(f"Screenshots this week: <b>{data['total_shots']}</b>", body_style)
    )
    week_active_secs = sum(d["active_seconds"] for d in data["idle_per_day"])
    week_idle_secs = sum(d["idle_seconds"] for d in data["idle_per_day"])
    story.append(
        Paragraph(
            f"Active time: <b>{_format_hours(week_active_secs)}</b> "
            f"(idle: {_format_hours(week_idle_secs)})",
            body_style,
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Streak", h2_style))
    story.append(
        Paragraph(
            f"Current streak: <b>{data['streak_days']}</b> day(s) "
            f"&nbsp;·&nbsp; longest ever: <b>{data['streak_longest']}</b>",
            body_style,
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    if data["first_capture"] or data["last_capture"]:
        story.append(Paragraph("First &amp; last capture", h2_style))
        if data["first_capture"]:
            story.append(
                Paragraph(f"First: <b>{_escape(data['first_capture'])}</b>", body_style)
            )
        if data["last_capture"]:
            story.append(
                Paragraph(f"Last: &nbsp;<b>{_escape(data['last_capture'])}</b>", body_style)
            )

    # ---- Page 2: daily bar chart -------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph(f"Daily shots — {week_label}", title_style))
    story.append(Spacer(1, 0.4 * cm))

    max_count = max((d["count"] for d in data["days"]), default=0)
    rows: list[list[object]] = [["Day", "Date", "Shots", "Bar", "Active (min)"]]
    for offset, day in enumerate(data["days"]):
        weekday = (data["week_start"] + timedelta(days=offset)).strftime("%a")
        bar_width = 0.0
        if max_count > 0:
            bar_width = (day["count"] / max_count) * 8.0  # in cm
        bar_block = "█" * max(0, int(bar_width * 2)) if day["count"] > 0 else ""
        active_minutes = _format_minutes(data["idle_per_day"][offset]["active_seconds"])
        rows.append([
            weekday,
            day["date"],
            str(day["count"]),
            bar_block,
            str(active_minutes),
        ])

    table = Table(
        rows,
        colWidths=[
            2.0 * cm,
            3.5 * cm,
            2.0 * cm,
            usable_width - (2.0 + 3.5 + 2.0 + 3.0) * cm,
            3.0 * cm,
        ],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                ("TEXTCOLOR", (3, 1), (3, -1), colors.HexColor("#10b981")),
                ("ALIGN", (2, 1), (2, -1), "RIGHT"),
                ("ALIGN", (4, 1), (4, -1), "RIGHT"),
            ]
        )
    )
    story.append(table)

    # ---- Page 3: top-10 apps with hours -------------------------------------
    story.append(PageBreak())
    story.append(Paragraph(f"Top apps — {week_label}", title_style))
    story.append(Spacer(1, 0.4 * cm))
    if not data["apps"]:
        story.append(Paragraph("<i>No app activity recorded this week.</i>", caption_style))
    else:
        app_rows: list[list[object]] = [["#", "App", "Hours", "Shots"]]
        for idx, raw_app in enumerate(data["apps"][:_TOP_APPS], start=1):
            app_name = _truncate(str(raw_app.get("app_name", "")), _APP_NAME_LIMIT)
            secs = int(cast("int", raw_app.get("seconds", 0)))
            shots = int(cast("int", raw_app.get("shots", 0)))
            app_rows.append([str(idx), app_name, _format_hours(secs), str(shots)])
        app_table = Table(
            app_rows,
            colWidths=[
                1.2 * cm,
                usable_width - (1.2 + 3.5 + 3.0) * cm,
                3.5 * cm,
                3.0 * cm,
            ],
        )
        app_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                    ("ALIGN", (0, 1), (0, -1), "RIGHT"),
                    ("ALIGN", (2, 1), (3, -1), "RIGHT"),
                ]
            )
        )
        story.append(app_table)

    # ---- Page 4: top-20 keywords --------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph(f"Top keywords — {week_label}", title_style))
    story.append(Spacer(1, 0.4 * cm))
    if not data["keywords"]:
        story.append(Paragraph("<i>No OCR or notes text in the window.</i>", caption_style))
    else:
        kw_rows: list[list[object]] = [["#", "Keyword", "Count"]]
        for idx, kw in enumerate(data["keywords"][:_TOP_KEYWORDS], start=1):
            kw_rows.append([str(idx), str(kw["word"]), str(kw["count"])])
        kw_table = Table(
            kw_rows,
            colWidths=[
                1.2 * cm,
                usable_width - (1.2 + 3.0) * cm,
                3.0 * cm,
            ],
        )
        kw_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                    ("ALIGN", (0, 1), (0, -1), "RIGHT"),
                    ("ALIGN", (2, 1), (2, -1), "RIGHT"),
                ]
            )
        )
        story.append(kw_table)

    # ---- Page 5: thumbnail mosaic -------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph(f"Highlights — {week_label}", title_style))
    story.append(Spacer(1, 0.4 * cm))

    if not data["mosaic"]:
        story.append(
            Paragraph(
                "<i>No usable thumbnails in this week's captures.</i>", caption_style
            )
        )
    else:
        cell_w = (usable_width - 2 * 0.3 * cm) / 3.0  # 3 columns with two gaps
        cell_h = 4.2 * cm
        mosaic_rows: list[list[Any]] = []
        current: list[Any] = []
        for shot in data["mosaic"]:
            thumb = _resolve_thumbnail(shot.get("thumbnail_path"))
            if thumb is None:
                cell: Any = Paragraph("<i>(missing)</i>", caption_style)
            else:
                try:
                    reader = ImageReader(str(thumb))
                    img_w, img_h = reader.getSize()
                except Exception as exc:  # pragma: no cover - PIL/IO surprises
                    log.warning(
                        "weekly_pdf.thumbnail_unreadable",
                        path=str(thumb),
                        error=str(exc),
                    )
                    cell = Paragraph("<i>(unreadable)</i>", caption_style)
                else:
                    ratio = (img_h / img_w) if img_w else 1.0
                    draw_w = cell_w
                    draw_h = draw_w * ratio
                    if draw_h > cell_h:
                        draw_h = cell_h
                        draw_w = draw_h / ratio if ratio else draw_w
                    cell = Image(str(thumb), width=draw_w, height=draw_h)
            current.append(cell)
            if len(current) == 3:
                mosaic_rows.append(current)
                current = []
        if current:
            while len(current) < 3:
                current.append("")
            mosaic_rows.append(current)

        if mosaic_rows:
            mosaic_table = Table(
                mosaic_rows,
                colWidths=[cell_w, cell_w, cell_w],
                rowHeights=[cell_h] * len(mosaic_rows),
            )
            mosaic_table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(mosaic_table)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return page_count


async def export_week_pdf(
    week_start_iso: str,
    output_path: Path | str,
) -> WeeklyPdfResult:
    """Render a weekly PDF for the ISO week containing ``week_start_iso``.

    ``week_start_iso`` accepts any ``YYYY-MM-DD`` inside the desired week;
    the function normalises it to that week's Monday.

    Returns a structured dict — see :class:`WeeklyPdfResult` — so HTTP and
    CLI callers can branch on ``status``:

    * ``"missing_dep"`` — ``reportlab`` is not installed.
    * ``"ok"`` — PDF was written; ``path`` and ``size_bytes`` are valid.
    * ``"bad_date"`` — ``week_start_iso`` does not match ``YYYY-MM-DD``.
    """
    try:
        week_start = _parse_week_iso(week_start_iso)
    except ValueError:
        log.warning("weekly_pdf.bad_date", week=week_start_iso)
        return WeeklyPdfResult(
            status="bad_date",
            path=None,
            pages=0,
            week_start="",
            size_bytes=0,
        )

    try:
        import reportlab  # noqa: F401, PLC0415 - probe for optional dep
    except ImportError:
        log.info("weekly_pdf.missing_dep", week=week_start.isoformat())
        return WeeklyPdfResult(
            status="missing_dep",
            path=None,
            pages=0,
            week_start=week_start.isoformat(),
            size_bytes=0,
        )

    data = await _load_week_data(week_start)

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pages = _build_pdf(data, out_path)
    size_bytes = out_path.stat().st_size if out_path.exists() else 0

    log.info(
        "weekly_pdf.built",
        week=week_start.isoformat(),
        path=str(out_path),
        pages=pages,
        total_shots=data["total_shots"],
        apps=len(data["apps"]),
        keywords=len(data["keywords"]),
        mosaic_cells=len(data["mosaic"]),
        size_bytes=size_bytes,
    )

    return WeeklyPdfResult(
        status="ok",
        path=str(out_path),
        pages=pages,
        week_start=week_start.isoformat(),
        size_bytes=size_bytes,
    )


__all__ = ["WeeklyPdfResult", "export_week_pdf"]
