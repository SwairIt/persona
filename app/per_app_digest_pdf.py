"""Per-app digest PDF — printable summary for one app over a date window.

Sibling to :mod:`app.web.routes.per_app_digest` (the HTML page). Pulls the
same per-app slice — capture count, window-title rollup, top OCR keywords,
recent activity stats — and lays it out as a single self-contained PDF for
offline / archival use.

The ``reportlab`` dependency is intentionally optional. When it is not
installed we fall back to ``weasyprint``; if neither is available the
function returns ``None`` and logs a single ``info`` line so the HTTP
route can convert that into a 503.
"""

from __future__ import annotations

import io
from collections import Counter
from typing import TYPE_CHECKING, Any, TypedDict

from app.keywords import STOPWORDS, _tokenise
from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Iterable

log = get_logger("persona.per_app_digest_pdf")

_TOP_WINDOWS = 10
_TOP_KEYWORDS = 20
_MIN_KEYWORD_LEN = 4
_WINDOW_TITLE_LIMIT = 70
_DEFAULT_DAYS = 7


class _AppData(TypedDict):
    """Aggregated slice for one app over the requested look-back window."""

    app_name: str
    days: int
    shots: int
    unique_windows: int
    ocr_chars: int
    active_hours: float
    window_rollup: list[tuple[str, int]]
    top_keywords: list[tuple[str, int]]


def _truncate(text: str, limit: int) -> str:
    """Flatten whitespace and cap to ``limit`` characters with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _escape(text: str) -> str:
    """Minimal HTML-escape for reportlab's ``Paragraph`` markup / weasyprint."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_hours(hours: float) -> str:
    """Render a fractional hour count as ``H:MM`` (no zero-padding on hours)."""
    if hours <= 0:
        return "0:00"
    whole = int(hours)
    minutes = round((hours - whole) * 60)
    if minutes == 60:
        whole += 1
        minutes = 0
    return f"{whole}:{minutes:02d}"


async def _load_app_data(app_name: str, days: int) -> _AppData | None:
    """Pull the aggregate slice for ``app_name`` over the last ``days`` days.

    Returns ``None`` when the app has never been captured (the route turns
    that into a 404). All SQL uses parametrised binds; date math uses
    SQLite's ``datetime('now', '-N days')`` form to stay timezone-agnostic
    relative to the database's clock.
    """
    if days <= 0:
        days = _DEFAULT_DAYS
    since_expr = f"-{int(days)} days"

    async with get_connection() as conn:
        # Guard: does the app exist at all (any time)?
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots WHERE app_name = ?",
            (app_name,),
        )
        guard_row = await cursor.fetchone()
        if guard_row is None or int(guard_row["n"]) == 0:
            return None

        # Aggregate stats inside the window.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS shots, "
            "COUNT(DISTINCT window_title) AS uniq, "
            "MIN(captured_at) AS first_at, "
            "MAX(captured_at) AS last_at "
            "FROM screenshots "
            "WHERE app_name = ? "
            "AND captured_at >= datetime('now', ?)",
            (app_name, since_expr),
        )
        agg_row = await cursor.fetchone()
        shots = int(agg_row["shots"]) if agg_row else 0
        unique_windows = int(agg_row["uniq"]) if agg_row else 0

        # Window-title rollup (top N inside the window).
        cursor = await conn.execute(
            "SELECT window_title, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE app_name = ? "
            "AND captured_at >= datetime('now', ?) "
            "AND window_title IS NOT NULL AND length(window_title) > 0 "
            "GROUP BY window_title ORDER BY n DESC LIMIT ?",
            (app_name, since_expr, _TOP_WINDOWS),
        )
        window_rollup: list[tuple[str, int]] = [
            (str(r["window_title"]), int(r["n"])) for r in await cursor.fetchall()
        ]

        # OCR text for keyword extraction + total char count.
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots "
            "WHERE app_name = ? "
            "AND captured_at >= datetime('now', ?) "
            "AND ocr_text IS NOT NULL AND ocr_text != ''",
            (app_name, since_expr),
        )
        ocr_rows: Iterable[Any] = await cursor.fetchall()

    counter: Counter[str] = Counter()
    ocr_chars = 0
    for row in ocr_rows:
        text = str(row["ocr_text"])
        ocr_chars += len(text)
        for token in _tokenise(text):
            if len(token) < _MIN_KEYWORD_LEN or token in STOPWORDS:
                continue
            counter[token] += 1
    top_keywords: list[tuple[str, int]] = list(counter.most_common(_TOP_KEYWORDS))

    # Heuristic "hours active" — a capture is taken roughly every minute, so
    # treat each shot as ~1 minute of activity, capped at 24h per day window.
    active_hours = min(float(days) * 24.0, shots / 60.0)

    return _AppData(
        app_name=app_name,
        days=int(days),
        shots=shots,
        unique_windows=unique_windows,
        ocr_chars=ocr_chars,
        active_hours=active_hours,
        window_rollup=window_rollup,
        top_keywords=top_keywords,
    )


def _build_pdf_reportlab(data: _AppData) -> bytes:
    """Render the digest with reportlab and return raw bytes."""
    from reportlab.lib import colors  # noqa: PLC0415 - optional dep, lazy
    from reportlab.lib.pagesizes import A4  # noqa: PLC0415
    from reportlab.lib.styles import (  # noqa: PLC0415
        ParagraphStyle,
        getSampleStyleSheet,
    )
    from reportlab.lib.units import cm  # noqa: PLC0415
    from reportlab.platypus import (  # noqa: PLC0415
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PerAppTitle",
        parent=styles["Title"],
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#1f2937"),
    )
    h2_style = ParagraphStyle(
        "PerAppH2",
        parent=styles["Heading2"],
        fontSize=14,
        leading=18,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "PerAppBody",
        parent=styles["BodyText"],
        fontSize=10.5,
        leading=14,
    )
    caption_style = ParagraphStyle(
        "PerAppCaption",
        parent=styles["BodyText"],
        fontSize=9.5,
        leading=12,
        textColor=colors.HexColor("#374151"),
    )
    bullet_style = ParagraphStyle(
        "PerAppBullet",
        parent=styles["BodyText"],
        fontSize=10,
        leading=13,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"Persona — {data['app_name']} digest",
        author="Persona",
    )
    page_width, _page_height = A4
    usable_width = page_width - doc.leftMargin - doc.rightMargin

    story: list[Any] = []

    # Title + period.
    story.append(
        Paragraph(
            f"Per-app digest — {_escape(data['app_name'])}",
            title_style,
        )
    )
    story.append(Spacer(1, 0.3 * cm))
    story.append(
        Paragraph(
            f"Period: last <b>{data['days']}</b> day(s)",
            caption_style,
        )
    )
    story.append(Spacer(1, 0.6 * cm))

    # 4-number top strip.
    strip_rows: list[list[object]] = [
        ["Shots", "Unique windows", "OCR chars", "Hours active"],
        [
            str(data["shots"]),
            str(data["unique_windows"]),
            str(data["ocr_chars"]),
            _format_hours(data["active_hours"]),
        ],
    ]
    strip_col = usable_width / 4.0
    strip = Table(
        strip_rows,
        colWidths=[strip_col] * 4,
    )
    strip.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 10),
                ("FONTSIZE", (0, 1), (-1, 1), 14),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 1), (-1, 1), 6),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
            ]
        )
    )
    story.append(strip)
    story.append(Spacer(1, 0.7 * cm))

    # Top window titles table.
    story.append(Paragraph("Top window titles", h2_style))
    if not data["window_rollup"]:
        story.append(
            Paragraph(
                "<i>No window titles recorded in this window.</i>",
                caption_style,
            )
        )
    else:
        win_rows: list[list[object]] = [["#", "Window title", "Shots"]]
        for idx, (title, count) in enumerate(data["window_rollup"], start=1):
            win_rows.append(
                [
                    str(idx),
                    _truncate(title, _WINDOW_TITLE_LIMIT),
                    str(count),
                ]
            )
        win_table = Table(
            win_rows,
            colWidths=[
                1.2 * cm,
                usable_width - (1.2 + 2.5) * cm,
                2.5 * cm,
            ],
        )
        win_table.setStyle(
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
        story.append(win_table)
    story.append(Spacer(1, 0.7 * cm))

    # Top OCR keywords as a bullet list.
    story.append(Paragraph("Top OCR keywords", h2_style))
    if not data["top_keywords"]:
        story.append(
            Paragraph(
                "<i>No OCR text in the window.</i>",
                caption_style,
            )
        )
    else:
        items = [
            ListItem(
                Paragraph(
                    f"{_escape(word)} &nbsp;<font color='#6b7280'>x{count}</font>",
                    bullet_style,
                ),
                leftIndent=10,
            )
            for word, count in data["top_keywords"]
        ]
        story.append(
            ListFlowable(
                items,
                bulletType="bullet",
                start="•",
                leftIndent=14,
            )
        )

    # Unused but harmless reference to keep body_style imported style available.
    _ = body_style

    doc.build(story)
    return buf.getvalue()


def _build_pdf_weasyprint(data: _AppData) -> bytes:
    """Render the digest with weasyprint and return raw bytes."""
    from weasyprint import HTML  # noqa: PLC0415 - optional dep, lazy

    window_rows = "".join(
        f"<tr><td class='num'>{idx}</td>"
        f"<td>{_escape(_truncate(title, _WINDOW_TITLE_LIMIT))}</td>"
        f"<td class='num'>{count}</td></tr>"
        for idx, (title, count) in enumerate(data["window_rollup"], start=1)
    ) or (
        "<tr><td colspan='3' class='muted'>"
        "No window titles recorded in this window.</td></tr>"
    )

    keyword_items = "".join(
        f"<li>{_escape(word)} <span class='muted'>x{count}</span></li>"
        for word, count in data["top_keywords"]
    ) or "<li class='muted'>No OCR text in the window.</li>"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Persona — {_escape(data['app_name'])} digest</title>
<style>
  @page {{ size: A4; margin: 2cm; }}
  body {{ font-family: Helvetica, Arial, sans-serif; color: #1f2937;
          font-size: 10.5pt; }}
  h1 {{ font-size: 22pt; margin: 0 0 4pt 0; color: #1f2937; }}
  h2 {{ font-size: 14pt; margin: 18pt 0 6pt 0; }}
  .caption {{ color: #374151; font-size: 9.5pt; margin-bottom: 14pt; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 8pt; }}
  th, td {{ border: 0.25pt solid #d1d5db; padding: 4pt 6pt;
            font-size: 10pt; }}
  th {{ background: #e5e7eb; color: #111827; text-align: left; }}
  .num {{ text-align: right; }}
  .muted {{ color: #6b7280; font-style: italic; }}
  .strip {{ display: table; width: 100%; border-collapse: collapse; }}
  .strip-cell {{ display: table-cell; width: 25%; text-align: center;
                 border: 0.25pt solid #d1d5db; padding: 6pt; }}
  .strip-cell .label {{ background: #e5e7eb; font-weight: bold;
                        font-size: 10pt; padding: 4pt; }}
  .strip-cell .value {{ font-size: 14pt; padding: 6pt; }}
  ul {{ padding-left: 18pt; }}
  li {{ margin-bottom: 2pt; }}
</style>
</head>
<body>
  <h1>Per-app digest — {_escape(data['app_name'])}</h1>
  <div class="caption">Period: last <b>{data['days']}</b> day(s)</div>

  <table class="strip">
    <tr>
      <th>Shots</th><th>Unique windows</th>
      <th>OCR chars</th><th>Hours active</th>
    </tr>
    <tr>
      <td class="num">{data['shots']}</td>
      <td class="num">{data['unique_windows']}</td>
      <td class="num">{data['ocr_chars']}</td>
      <td class="num">{_format_hours(data['active_hours'])}</td>
    </tr>
  </table>

  <h2>Top window titles</h2>
  <table>
    <tr><th>#</th><th>Window title</th><th>Shots</th></tr>
    {window_rows}
  </table>

  <h2>Top OCR keywords</h2>
  <ul>{keyword_items}</ul>
</body>
</html>
"""
    return bytes(HTML(string=html).write_pdf() or b"")


async def build_per_app_digest_pdf(
    app_name: str,
    days: int = _DEFAULT_DAYS,
) -> bytes | None:
    """Render a per-app digest PDF and return the bytes (or ``None``).

    Returns:
        * ``bytes`` — the rendered PDF when a backend is available and
          the app has at least one capture in the database.
        * ``None`` when neither ``reportlab`` nor ``weasyprint`` is
          installed (a single ``info`` log line records the miss), *or*
          when ``app_name`` has never been captured.

    Args:
        app_name: Exact ``screenshots.app_name`` value to filter on.
        days: Look-back window in days (defaults to 7). Non-positive
            values are coerced to the default to keep the SQL safe.
    """
    if days <= 0:
        days = _DEFAULT_DAYS

    data = await _load_app_data(app_name, days)
    if data is None:
        log.info(
            "per_app_digest_pdf.unknown_app",
            app_name=app_name,
            days=days,
        )
        return None

    backend: str | None = None
    try:
        import reportlab  # noqa: F401, PLC0415 - probe optional dep
    except ImportError:
        pass
    else:
        backend = "reportlab"

    if backend is None:
        try:
            import weasyprint  # noqa: F401, PLC0415 - probe optional dep
        except ImportError:
            log.info(
                "per_app_digest_pdf.missing_backend",
                app_name=app_name,
                days=days,
            )
            return None
        backend = "weasyprint"

    if backend == "reportlab":
        pdf_bytes = _build_pdf_reportlab(data)
    else:
        pdf_bytes = _build_pdf_weasyprint(data)

    log.info(
        "per_app_digest_pdf.built",
        app_name=app_name,
        days=days,
        backend=backend,
        shots=data["shots"],
        unique_windows=data["unique_windows"],
        ocr_chars=data["ocr_chars"],
        windows=len(data["window_rollup"]),
        keywords=len(data["top_keywords"]),
        size_bytes=len(pdf_bytes),
    )
    return pdf_bytes


__all__ = ["build_per_app_digest_pdf"]
