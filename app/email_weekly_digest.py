"""Weekly email digest body builder (v1.59).

Composes the HTML body for a Sunday-evening recap email that summarises
the last seven days of activity. Pulls from three existing surfaces
already shipped:

* ``weekly_card.llm_summary`` (migration ``119_weekly_card_llm.sql``)
  — the LLM-written narrative paragraph for the target ISO week.
* ``weekly_highlight`` (migration ``126_weekly_highlights.sql``)
  — the 5-7 curated picks, ``rank``-ordered.
* Four-stat delta strip (shots / voice / apps / notes), computed
  with the same noise-floor / signed-percent shape that
  :mod:`app.monthly_comparison` uses, but over a *weekly* window:
  this week vs the same week one calendar month ago.

The output is a single self-contained HTML string with inline CSS so
it renders identically in Gmail / Apple Mail / desktop Outlook (none of
which honour ``<style>`` blocks reliably). The structure is two nested
tables — :class:`MJML`-style "rows of single-cell tables" — which is
the only layout primitive that survives every major mail renderer.

Side effects
------------

This module is read-only against the database. It opens a single
aiosqlite connection, runs four parametrised SELECTs (one per surface)
and returns the rendered string. Callers (the worker, the preview
endpoint) are responsible for actually shipping the body via SMTP.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape as _html_escape
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.email_weekly_digest")

# Number of headline shots / notes the digest links out to in the
# four-stat strip and the highlights list. The highlight LLM is
# prompted for 5-7 picks; we surface whatever it produced and cap at 7.
_MAX_HIGHLIGHTS: int = 7

# Public link target for the curated highlights page. The footer
# nudges the reader to click through for the full list rather than
# reading a 30-card email.
_HIGHLIGHTS_URL: str = "/memory/highlights"

# Brand palette — matches the dark accent the in-app UI uses. Inlined
# directly in the templates below because <style> blocks are stripped
# by Gmail's mobile clipper.
_BG_OUTER: str = "#0b0d10"
_BG_CARD: str = "#16191e"
_BG_CARD_ALT: str = "#1c2026"
_TEXT_PRIMARY: str = "#e4e4e7"
_TEXT_MUTED: str = "#a1a1aa"
_TEXT_DIM: str = "#71717a"
_ACCENT: str = "#3b82f6"
_DELTA_POSITIVE: str = "#22c55e"
_DELTA_NEGATIVE: str = "#ef4444"
_BORDER: str = "#2a2f37"


def _monday_of(when: date) -> date:
    """Return the Monday of the ISO week containing ``when``."""
    return when - timedelta(days=when.weekday())


def _parse_week_start(week_start_iso: str) -> date:
    """Parse the caller-supplied YYYY-MM-DD string into a real ``date``.

    Falls back to "Monday of the current ISO week" so a malformed
    caller never breaks the email — the digest is still useful even if
    the worker passed in garbage.
    """
    try:
        parsed = date.fromisoformat(week_start_iso.strip())
    except (AttributeError, ValueError):
        log.warning("email_weekly_digest.bad_week_start", value=week_start_iso)
        parsed = datetime.now().astimezone().date()
    return _monday_of(parsed)


async def _load_llm_summary(
    conn: aiosqlite.Connection, *, week_start_iso: str
) -> str:
    """Return ``weekly_card.llm_summary`` for the week or an empty string.

    Treats ``NULL`` / whitespace-only values as "no summary" so the
    template can render a neutral fallback rather than the literal
    string ``"None"``.
    """
    cursor = await conn.execute(
        "SELECT llm_summary FROM weekly_card WHERE week_start = ?",
        (week_start_iso,),
    )
    row = await cursor.fetchone()
    if row is None:
        return ""
    raw = row["llm_summary"]
    if raw is None:
        return ""
    return str(raw).strip()


async def _load_highlights(
    conn: aiosqlite.Connection, *, week_start_iso: str
) -> list[dict[str, Any]]:
    """Return up to :data:`_MAX_HIGHLIGHTS` picks for the week, rank-ordered.

    Each entry carries a ``thumbnail_path`` lookup result for shot/note
    picks so the email can show a small preview image. Sessions have
    no canonical thumb so the column is ``None`` for them.
    """
    cursor = await conn.execute(
        "SELECT id, rank, source_kind, source_id, title, reason "
        "FROM weekly_highlight "
        "WHERE week_start = ? "
        "ORDER BY rank ASC LIMIT ?",
        (week_start_iso, _MAX_HIGHLIGHTS),
    )
    rows = await cursor.fetchall()
    picks: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row["source_kind"])
        sid = int(row["source_id"])
        thumb: str | None = None
        if kind in {"shot", "note"}:
            thumb = await _lookup_shot_thumb(conn, sid)
        picks.append(
            {
                "id": int(row["id"]),
                "rank": int(row["rank"]),
                "source_kind": kind,
                "source_id": sid,
                "title": str(row["title"]),
                "reason": str(row["reason"]),
                "thumbnail_path": thumb,
            }
        )
    return picks


async def _lookup_shot_thumb(
    conn: aiosqlite.Connection, screenshot_id: int
) -> str | None:
    """Return the absolute thumbnail path for a screenshot if it still exists."""
    cursor = await conn.execute(
        "SELECT thumbnail_path FROM screenshots WHERE id = ?",
        (int(screenshot_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    raw = row["thumbnail_path"]
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _thumb_url(thumbnail_path: str | None) -> str | None:
    """Map a stored thumbnail path to its public ``/thumbs/...`` URL.

    Lazy-imports :func:`app.web.routes.thumbnails.thumbnail_url` to
    avoid a circular import when this module is imported from the
    worker (which boots before the web app).
    """
    if thumbnail_path is None:
        return None
    try:
        from app.web.routes.thumbnails import (  # noqa: PLC0415
            thumbnail_url,
        )
    except ImportError:
        return None
    return thumbnail_url(thumbnail_path)


async def _load_weekly_delta(week_start_iso: str) -> dict[str, float]:
    """Return four ``{shots,voice,apps,notes}`` deltas this-week vs prior-month-same-week.

    Re-uses :func:`app.monthly_comparison.compute_comparison` — that
    helper already knows how to express change as a signed percentage
    with a noise-floor. We invoke it twice (this month, prior month)
    and pick the deltas off, scoping the *day window* to the seven
    days of the target week rather than the whole calendar month.
    """
    week_start = _parse_week_start(week_start_iso)
    week_end_exclusive = week_start + timedelta(days=7)
    prior_start = _shift_back_by_month(week_start)
    prior_end_exclusive = prior_start + timedelta(days=7)

    async with get_connection() as conn:
        this_totals = await _window_totals(
            conn,
            start_iso=week_start.isoformat(),
            end_iso=week_end_exclusive.isoformat(),
        )
        last_totals = await _window_totals(
            conn,
            start_iso=prior_start.isoformat(),
            end_iso=prior_end_exclusive.isoformat(),
        )

    return {
        "shots": _delta_pct(this_totals["shots"], last_totals["shots"]),
        "voice": _delta_pct(this_totals["voice"], last_totals["voice"]),
        "apps": _delta_pct(this_totals["apps"], last_totals["apps"]),
        "notes": _delta_pct(this_totals["notes"], last_totals["notes"]),
    }


def _shift_back_by_month(when: date) -> date:
    """Return the same calendar slot one calendar month earlier.

    A naive ``timedelta(days=28)`` drifts; ``relativedelta`` is not in
    the stdlib. We hand-roll a month subtract — clamp the day to the
    prior month's length so e.g. ``2026-03-31 → 2026-02-28``.
    """
    year = when.year
    month = when.month - 1
    if month == 0:
        month = 12
        year -= 1
    # Clamp day to prior-month length.
    import calendar  # noqa: PLC0415 — only needed for the rare clamp branch

    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(when.day, last_day))


async def _window_totals(
    conn: aiosqlite.Connection,
    *,
    start_iso: str,
    end_iso: str,
) -> dict[str, float]:
    """Sum the four headline counters across a half-open day window."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ?",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    shots = float(row["n"]) if row is not None else 0.0

    cursor = await conn.execute(
        "SELECT COALESCE(SUM(duration_seconds), 0.0) AS total "
        "FROM audio_segment "
        "WHERE captured_at >= ? AND captured_at < ?",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    voice = float(row["total"]) if row is not None else 0.0

    cursor = await conn.execute(
        "SELECT COUNT(DISTINCT app_name) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "  AND app_name IS NOT NULL AND app_name != ''",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    apps = float(row["n"]) if row is not None else 0.0

    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM notes "
        "WHERE created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    notes = float(row["n"]) if row is not None else 0.0

    return {"shots": shots, "voice": voice, "apps": apps, "notes": notes}


def _delta_pct(this_value: float, last_value: float) -> float:
    """Percentage delta of ``this_value`` vs ``last_value``.

    Mirrors :func:`app.monthly_comparison._delta_pct` — special-cases
    a zero baseline so a fresh install does not divide-by-zero, and
    rounds to one decimal so the template needs no math.
    """
    if last_value <= 0.0:
        if this_value > 0.0:
            return 100.0
        return 0.0
    return round(((this_value - last_value) / last_value) * 100.0, 1)


def _fmt_delta(delta: float) -> tuple[str, str]:
    """Return ``(display_text, hex_colour)`` for a signed percentage."""
    if delta > 0:
        return f"+{delta:.1f}%", _DELTA_POSITIVE
    if delta < 0:
        return f"{delta:.1f}%", _DELTA_NEGATIVE
    return "0.0%", _TEXT_MUTED


def _render_stat_cell(*, label: str, value: float) -> str:
    """Render one cell of the four-stat delta strip."""
    text, colour = _fmt_delta(value)
    safe_label = _html_escape(label)
    safe_text = _html_escape(text)
    return (
        f'<td align="center" valign="middle" '
        f'style="padding:12px 8px; background-color:{_BG_CARD_ALT}; '
        f'border:1px solid {_BORDER}; border-radius:6px; '
        f'font-family:-apple-system,Segoe UI,Roboto,sans-serif;">'
        f'<div style="font-size:11px; color:{_TEXT_DIM}; '
        f'text-transform:uppercase; letter-spacing:0.06em; '
        f'margin-bottom:6px;">{safe_label}</div>'
        f'<div style="font-size:20px; color:{colour}; '
        f'font-weight:600; font-variant-numeric:tabular-nums;">'
        f"{safe_text}</div></td>"
    )


def _render_highlight_card(pick: dict[str, Any]) -> str:
    """Render one highlight as a mobile-friendly table card."""
    kind = pick["source_kind"]
    safe_title = _html_escape(pick["title"])
    safe_reason = _html_escape(pick["reason"])
    rank = int(pick["rank"])
    sid = int(pick["source_id"])

    # Both shot and note picks route to /screenshot/<id>; sessions
    # don't have a canonical detail page yet so we render the title
    # as plain text.
    if kind in {"shot", "note"}:
        title_html = (
            f'<a href="/screenshot/{sid}" '
            f'style="color:{_TEXT_PRIMARY}; text-decoration:none; '
            f'font-weight:600;">{safe_title}</a>'
        )
    else:
        title_html = (
            f'<span style="color:{_TEXT_PRIMARY}; '
            f'font-weight:600;">{safe_title}</span>'
        )

    thumb_html = ""
    thumb_url = _thumb_url(pick["thumbnail_path"])
    if thumb_url:
        safe_thumb = _html_escape(thumb_url, quote=True)
        thumb_html = (
            f'<td valign="top" width="72" '
            f'style="padding-right:12px;">'
            f'<a href="/screenshot/{sid}">'
            f'<img src="{safe_thumb}" width="64" height="64" '
            f'alt="" style="display:block; border-radius:6px; '
            f'border:1px solid {_BORDER}; '
            f'object-fit:cover;"></a></td>'
        )

    return (
        f'<tr><td style="padding:8px 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0" '
        f'style="background-color:{_BG_CARD_ALT}; '
        f'border:1px solid {_BORDER}; border-radius:8px;">'
        f'<tr><td style="padding:14px 16px;">'
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0">'
        f"<tr>{thumb_html}"
        f'<td valign="top">'
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,'
        f'sans-serif; font-size:11px; color:{_TEXT_DIM}; '
        f'text-transform:uppercase; letter-spacing:0.06em; '
        f'margin-bottom:4px;">#{rank} &middot; {_html_escape(kind)}</div>'
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,'
        f'sans-serif; font-size:15px; line-height:1.35; '
        f'margin-bottom:6px;">{title_html}</div>'
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,'
        f'sans-serif; font-size:13px; color:{_TEXT_MUTED}; '
        f'line-height:1.45;">{safe_reason}</div>'
        f"</td></tr></table></td></tr></table></td></tr>"
    )


def _render_body(
    *,
    week_start_iso: str,
    llm_summary: str,
    highlights: list[dict[str, Any]],
    deltas: dict[str, float],
) -> str:
    """Compose the full HTML body. Self-contained, no <style> blocks."""
    safe_week = _html_escape(week_start_iso)
    safe_summary = _html_escape(llm_summary) if llm_summary else (
        "No LLM-written summary for this week yet — flip "
        "<code>weekly_card_llm_enabled</code> to populate it."
    )

    highlight_rows = (
        "".join(_render_highlight_card(p) for p in highlights)
        if highlights
        else (
            f'<tr><td style="padding:14px 16px; '
            f'background-color:{_BG_CARD_ALT}; border:1px solid {_BORDER}; '
            f'border-radius:8px; color:{_TEXT_MUTED}; '
            f'font-family:-apple-system,Segoe UI,Roboto,sans-serif; '
            f'font-size:13px;">No curated highlights for this week. '
            f"Run the weekly-highlights worker to fill them in.</td></tr>"
        )
    )

    stat_cells = "".join(
        [
            _render_stat_cell(label="Shots", value=deltas["shots"]),
            _render_stat_cell(label="Voice", value=deltas["voice"]),
            _render_stat_cell(label="Apps", value=deltas["apps"]),
            _render_stat_cell(label="Notes", value=deltas["notes"]),
        ]
    )

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Persona weekly digest &mdash; {safe_week}</title>"
        f'</head><body style="margin:0; padding:0; '
        f'background-color:{_BG_OUTER}; '
        f'color:{_TEXT_PRIMARY}; '
        f'font-family:-apple-system,Segoe UI,Roboto,sans-serif;">'
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0" '
        f'style="background-color:{_BG_OUTER};">'
        f'<tr><td align="center" style="padding:24px 12px;">'
        f'<table role="presentation" width="600" cellpadding="0" '
        f'cellspacing="0" border="0" '
        f'style="max-width:600px; width:100%; '
        f'background-color:{_BG_CARD}; border-radius:10px; '
        f'border:1px solid {_BORDER};">'
        # Heading
        f'<tr><td style="padding:24px 24px 12px 24px;">'
        f'<div style="font-size:11px; color:{_ACCENT}; '
        f'text-transform:uppercase; letter-spacing:0.08em; '
        f'margin-bottom:6px;">Persona</div>'
        f'<h1 style="margin:0; font-size:22px; line-height:1.3; '
        f'color:{_TEXT_PRIMARY};">'
        f"Persona weekly digest &mdash; week of {safe_week}</h1></td></tr>"
        # LLM summary
        f'<tr><td style="padding:8px 24px 16px 24px; '
        f'color:{_TEXT_MUTED}; font-size:14px; line-height:1.55;">'
        f"<p style=\"margin:0;\">{safe_summary}</p></td></tr>"
        # Highlights
        f'<tr><td style="padding:8px 24px 0 24px;">'
        f'<div style="font-size:11px; color:{_TEXT_DIM}; '
        f'text-transform:uppercase; letter-spacing:0.08em; '
        f'margin-bottom:10px;">Highlights</div>'
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0">'
        f"{highlight_rows}"
        f"</table></td></tr>"
        # Stats strip
        f'<tr><td style="padding:20px 24px 8px 24px;">'
        f'<div style="font-size:11px; color:{_TEXT_DIM}; '
        f'text-transform:uppercase; letter-spacing:0.08em; '
        f'margin-bottom:10px;">vs same week last month</div>'
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="6" border="0">'
        f"<tr>{stat_cells}</tr></table></td></tr>"
        # Footer
        f'<tr><td style="padding:20px 24px 24px 24px; '
        f'border-top:1px solid {_BORDER}; color:{_TEXT_DIM}; '
        f'font-size:12px; line-height:1.5;">'
        f'<a href="{_HIGHLIGHTS_URL}" '
        f'style="color:{_ACCENT}; text-decoration:none;">'
        f"Open all highlights &rarr;</a><br><br>"
        f"Unsubscribe: open Persona &rarr; Settings &rarr; "
        f"Weekly email digest and untick &quot;Enabled&quot;. "
        f"Or set <code>email_weekly_digest_enabled=0</code> "
        f"in <code>kv_settings</code>.</td></tr>"
        f"</table></td></tr></table></body></html>"
    )


async def build_weekly_digest_body(week_start_iso: str) -> str:
    """Render the full HTML body for the weekly digest email.

    Args:
        week_start_iso: ``YYYY-MM-DD`` of the Monday of the target ISO
            week. Any day inside the week is also accepted — the
            Monday is computed automatically.

    Returns:
        A single self-contained HTML string, safe to ship as the
        ``body_html`` argument to :func:`app.smtp_delivery.send_digest_email`
        (the plaintext fallback is left to the caller).
    """
    week_start = _parse_week_start(week_start_iso)
    week_start_str = week_start.isoformat()

    async with get_connection() as conn:
        llm_summary = await _load_llm_summary(
            conn, week_start_iso=week_start_str
        )
        highlights = await _load_highlights(
            conn, week_start_iso=week_start_str
        )

    deltas = await _load_weekly_delta(week_start_str)

    log.info(
        "email_weekly_digest.compose",
        week_start=week_start_str,
        has_llm_summary=bool(llm_summary),
        highlights=len(highlights),
        delta_shots=deltas["shots"],
        delta_voice=deltas["voice"],
        delta_apps=deltas["apps"],
        delta_notes=deltas["notes"],
    )

    return _render_body(
        week_start_iso=week_start_str,
        llm_summary=llm_summary,
        highlights=highlights,
        deltas=deltas,
    )


__all__ = ["build_weekly_digest_body"]
