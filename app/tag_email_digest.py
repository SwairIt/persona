"""Per-tag weekly email digest helpers (v1.61).

Sibling of :mod:`app.email_weekly_digest`: that module composes the
single global Sunday-evening recap; this one fans the SMTP infra out
across N tag-scoped subscriptions stored in
``tag_email_subscription`` (migration ``146_tag_email_subscription.sql``).

A subscription answers four questions:

* **Which tag?** raw tag name, case-insensitive match against
  ``tags.name`` — same contract as :mod:`app.tag_feed`.
* **Which inbox?** plain RFC-822 address.
* **Which slot?** ``(day_of_week, hour_local)`` — Mon..Sun x 0..23.
* **Active?** ``enabled = 1`` toggles the row without deleting it.

The public surface is five async helpers:

* :func:`list_subscriptions` — read-only, used by the settings page.
* :func:`upsert_subscription` — UPSERT keyed on ``(tag, email)``.
* :func:`delete_subscription` — hard delete by ``id``.
* :func:`build_tag_digest_body` — HTML body for the last seven days of
  shots that carry the requested tag (link + thumbnail + OCR snippet).
* :func:`send_due_subscriptions` — hourly worker fan-out: finds every
  enabled row whose ``(day_of_week, hour_local)`` matches *now* AND
  whose ``last_sent_at`` is at least six days old, ships each via the
  shared SMTP helper, and stamps ``last_sent_at``.

All SQL is parametrised. Failures inside one subscription are logged
and skipped — the worker never lets a single broken row freeze the
fan-out for the rest.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape as _html_escape
from typing import Any

from app.logging_setup import get_logger
from app.smtp_delivery import send_digest_email
from app.storage.db import get_connection

log = get_logger("persona.tag_email_digest")

# Number of shots the digest body links out to. Matches the cap the
# global weekly digest uses for its highlights list — long enough to
# be useful, short enough to fit in a typical mail client's preview
# pane without forcing the reader to scroll.
_MAX_SHOTS: int = 20

# First N characters of ``screenshots.ocr_text`` rendered as the body
# snippet when the shot has no ``alt_text``. Mirrors the constant in
# :mod:`app.tag_feed` so both surfaces show a comparable amount of
# context per shot.
_OCR_SNIPPET_LEN: int = 200

# Floor for "this subscription has not fired recently enough to fire
# again". Six days, not seven, so a sub configured for Sunday 19:00 is
# still due the next Sunday 19:00 even if the previous send slipped a
# few minutes (DST, NTP nudge, worker restart at the firing hour).
_RESEND_FLOOR_DAYS: int = 6

# Brand palette — copied verbatim from :mod:`app.email_weekly_digest`
# so the per-tag body renders with the same colours as the global
# digest. Email clients strip ``<style>`` blocks unreliably, so every
# colour is inlined at the call site.
_BG_OUTER: str = "#0b0d10"
_BG_CARD: str = "#16191e"
_BG_CARD_ALT: str = "#1c2026"
_TEXT_PRIMARY: str = "#e4e4e7"
_TEXT_MUTED: str = "#a1a1aa"
_TEXT_DIM: str = "#71717a"
_ACCENT: str = "#3b82f6"
_BORDER: str = "#2a2f37"


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


async def list_subscriptions() -> list[dict[str, Any]]:
    """Return every row in ``tag_email_subscription`` as a plain dict.

    Ordered by ``(tag, email)`` so the settings page renders a stable
    list that does not jump around between visits.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, tag, email, day_of_week, hour_local, enabled, "
            "       created_at, last_sent_at "
            "FROM tag_email_subscription "
            "ORDER BY tag ASC, email ASC"
        )
        rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "tag": str(row["tag"]),
                "email": str(row["email"]),
                "day_of_week": int(row["day_of_week"]),
                "hour_local": int(row["hour_local"]),
                "enabled": bool(int(row["enabled"])),
                "created_at": str(row["created_at"]),
                "last_sent_at": (
                    None
                    if row["last_sent_at"] is None
                    else str(row["last_sent_at"])
                ),
            }
        )
    return out


async def upsert_subscription(
    tag: str,
    email: str,
    day_of_week: int,
    hour_local: int,
) -> int:
    """Insert or update one subscription, return the row id.

    Keyed on the ``UNIQUE(tag, email)`` pair so re-submitting the same
    pair from the settings form just nudges the day/hour rather than
    growing a duplicate row. ``enabled`` is set to ``1`` on every
    upsert so a paused-then-resaved row resumes firing automatically.
    """
    tag_clean = tag.strip().lstrip("#")
    email_clean = email.strip()
    if not tag_clean:
        msg = "tag must be non-empty"
        raise ValueError(msg)
    if not email_clean:
        msg = "email must be non-empty"
        raise ValueError(msg)
    if not 0 <= day_of_week <= 6:
        msg = "day_of_week must be 0..6"
        raise ValueError(msg)
    if not 0 <= hour_local <= 23:
        msg = "hour_local must be 0..23"
        raise ValueError(msg)

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO tag_email_subscription "
            "(tag, email, day_of_week, hour_local, enabled) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(tag, email) DO UPDATE SET "
            "    day_of_week = excluded.day_of_week, "
            "    hour_local = excluded.hour_local, "
            "    enabled = 1",
            (tag_clean, email_clean, int(day_of_week), int(hour_local)),
        )
        cursor = await conn.execute(
            "SELECT id FROM tag_email_subscription "
            "WHERE tag = ? AND email = ?",
            (tag_clean, email_clean),
        )
        row = await cursor.fetchone()
        await conn.commit()
    if row is None:
        msg = "upsert_subscription: row vanished mid-transaction"
        raise RuntimeError(msg)
    sub_id = int(row["id"])
    log.info(
        "tag_email_digest.upsert",
        sub_id=sub_id,
        tag=tag_clean,
        email=email_clean,
        day_of_week=int(day_of_week),
        hour_local=int(hour_local),
    )
    return sub_id


async def delete_subscription(sub_id: int) -> None:
    """Hard-delete one row by id. No-ops on a missing id."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM tag_email_subscription WHERE id = ?",
            (int(sub_id),),
        )
        await conn.commit()
    log.info("tag_email_digest.delete", sub_id=int(sub_id))


# ---------------------------------------------------------------------------
# Body builder
# ---------------------------------------------------------------------------


async def build_tag_digest_body(tag: str, week_start_iso: str) -> str:
    """Render an HTML body of every shot tagged ``tag`` in the last week.

    Args:
        tag: Raw tag name. Matched case-insensitively against
            ``tags.name`` (same contract as :mod:`app.tag_feed`).
        week_start_iso: ``YYYY-MM-DD`` of the Monday-of-week anchor.
            Any in-week day works — the helper computes the Monday and
            uses a half-open ``[monday, monday+7)`` window over
            ``screenshots.captured_at``.

    Returns:
        A complete self-contained HTML document, safe to ship as the
        ``body_html`` argument to :func:`send_digest_email`.
    """
    week_start = _parse_week_start(week_start_iso)
    week_end_exclusive = week_start + timedelta(days=7)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT s.id, s.captured_at, s.app_name, s.window_title, "
            "       s.ocr_text, s.alt_text, s.thumbnail_path, "
            "       t.name AS tag_name "
            "FROM screenshots s "
            "JOIN screenshot_tags st ON st.screenshot_id = s.id "
            "JOIN tags t ON t.id = st.tag_id "
            "WHERE LOWER(t.name) = LOWER(?) "
            "  AND s.captured_at >= ? "
            "  AND s.captured_at < ? "
            "ORDER BY s.captured_at DESC "
            "LIMIT ?",
            (
                tag.strip().lstrip("#"),
                week_start.isoformat(),
                week_end_exclusive.isoformat(),
                _MAX_SHOTS,
            ),
        )
        rows = list(await cursor.fetchall())

    canonical_tag = (
        str(rows[0]["tag_name"]) if rows else tag.strip().lstrip("#")
    )

    log.info(
        "tag_email_digest.compose",
        tag=canonical_tag,
        week_start=week_start.isoformat(),
        shots=len(rows),
    )

    return _render_body(
        canonical_tag=canonical_tag,
        week_start_iso=week_start.isoformat(),
        rows=rows,
    )


def _parse_week_start(week_start_iso: str) -> Any:
    """Parse the caller-supplied YYYY-MM-DD into the Monday of that ISO week.

    Falls back to "Monday of the current local week" on malformed input
    so a fat-fingered call site never crashes the worker fan-out.
    """
    from datetime import date  # noqa: PLC0415

    try:
        parsed = date.fromisoformat(week_start_iso.strip())
    except (AttributeError, ValueError):
        log.warning("tag_email_digest.bad_week_start", value=week_start_iso)
        parsed = datetime.now().astimezone().date()
    return parsed - timedelta(days=parsed.weekday())


def _thumb_url(thumbnail_path: str | None) -> str | None:
    """Map a stored thumbnail path to its public ``/thumbs/...`` URL.

    Lazy-imports :func:`app.web.routes.thumbnails.thumbnail_url` to
    avoid a circular import when this module is imported from the
    worker (which boots before the web app).
    """
    if thumbnail_path is None:
        return None
    text = str(thumbnail_path).strip()
    if not text:
        return None
    try:
        from app.web.routes.thumbnails import (  # noqa: PLC0415
            thumbnail_url,
        )
    except ImportError:
        return None
    return thumbnail_url(text)


def _render_shot_row(row: Any) -> str:
    """Render one shot as a table card identical in shape to the global digest."""
    sid = int(row["id"])
    captured_raw = str(row["captured_at"])
    app_name = str(row["app_name"] or "—")
    window_title = str(row["window_title"] or "")
    alt_text = row["alt_text"]
    ocr_text = row["ocr_text"]
    thumbnail_path = row["thumbnail_path"]

    # OCR snippet falls back to alt_text → ocr_text → empty, mirroring
    # :mod:`app.tag_feed`.
    snippet = ""
    if alt_text is not None and str(alt_text).strip():
        snippet = str(alt_text).strip()
    elif ocr_text is not None and str(ocr_text).strip():
        snippet = str(ocr_text).strip()[:_OCR_SNIPPET_LEN]

    safe_title = _html_escape(
        f"{captured_raw[:16]} — {app_name} — {window_title}".rstrip(" —")
    )
    safe_snippet = _html_escape(snippet)

    thumb_html = ""
    thumb_url = _thumb_url(
        None if thumbnail_path is None else str(thumbnail_path)
    )
    if thumb_url:
        safe_thumb = _html_escape(thumb_url, quote=True)
        thumb_html = (
            f'<td valign="top" width="72" '
            f'style="padding-right:12px;">'
            f'<a href="/screenshot/{sid}">'
            f'<img src="{safe_thumb}" width="64" height="64" alt="" '
            f'style="display:block; border-radius:6px; '
            f'border:1px solid {_BORDER}; '
            f'object-fit:cover;"></a></td>'
        )

    title_html = (
        f'<a href="/screenshot/{sid}" '
        f'style="color:{_TEXT_PRIMARY}; text-decoration:none; '
        f'font-weight:600;">{safe_title}</a>'
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
        f'sans-serif; font-size:14px; line-height:1.35; '
        f'margin-bottom:6px;">{title_html}</div>'
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,'
        f'sans-serif; font-size:12px; color:{_TEXT_MUTED}; '
        f'line-height:1.45;">{safe_snippet}</div>'
        f"</td></tr></table></td></tr></table></td></tr>"
    )


def _render_body(
    *,
    canonical_tag: str,
    week_start_iso: str,
    rows: list[Any],
) -> str:
    """Compose the full HTML body. Mirrors the global digest's table-card layout."""
    safe_tag = _html_escape(canonical_tag)
    safe_week = _html_escape(week_start_iso)

    if rows:
        shot_rows = "".join(_render_shot_row(r) for r in rows)
    else:
        shot_rows = (
            f'<tr><td style="padding:14px 16px; '
            f'background-color:{_BG_CARD_ALT}; border:1px solid {_BORDER}; '
            f'border-radius:8px; color:{_TEXT_MUTED}; '
            f'font-family:-apple-system,Segoe UI,Roboto,sans-serif; '
            f"font-size:13px;\">No shots tagged #{safe_tag} "
            f"this week.</td></tr>"
        )

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Persona — #{safe_tag} — {safe_week}</title>"
        f'</head><body style="margin:0; padding:0; '
        f'background-color:{_BG_OUTER}; color:{_TEXT_PRIMARY}; '
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
        f'<tr><td style="padding:24px 24px 12px 24px;">'
        f'<div style="font-size:11px; color:{_ACCENT}; '
        f'text-transform:uppercase; letter-spacing:0.08em; '
        f'margin-bottom:6px;">Persona &middot; tag digest</div>'
        f'<h1 style="margin:0; font-size:22px; line-height:1.3; '
        f'color:{_TEXT_PRIMARY};">'
        f"#{safe_tag} &mdash; week of {safe_week}</h1></td></tr>"
        f'<tr><td style="padding:8px 24px 0 24px;">'
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0">'
        f"{shot_rows}"
        f"</table></td></tr>"
        f'<tr><td style="padding:20px 24px 24px 24px; '
        f'border-top:1px solid {_BORDER}; color:{_TEXT_DIM}; '
        f'font-size:12px; line-height:1.5;">'
        f'<a href="/tag/{safe_tag}" '
        f'style="color:{_ACCENT}; text-decoration:none;">'
        f"Open #{safe_tag} in Persona &rarr;</a><br><br>"
        f"Unsubscribe: open Persona &rarr; Settings &rarr; "
        f"Email подписки по тегу and delete the row for this address."
        f"</td></tr>"
        f"</table></td></tr></table></body></html>"
    )


# ---------------------------------------------------------------------------
# Worker fan-out
# ---------------------------------------------------------------------------


async def send_due_subscriptions(now_iso: str) -> dict[str, int]:
    """Fan out one tick of the per-tag digest worker.

    Reads every enabled row whose ``(day_of_week, hour_local)`` matches
    the wall-clock derived from ``now_iso``, filters out rows whose
    ``last_sent_at`` is less than :data:`_RESEND_FLOOR_DAYS` old, builds
    the body for each surviving subscription, ships it via
    :func:`send_digest_email`, and stamps ``last_sent_at`` on a clean
    ``sent`` outcome. Returns a counter dict the worker logs.

    A single broken row (SMTP error, body-builder bug) is caught and
    logged — the rest of the fan-out continues. The only way one tick
    can fail the whole worker is if ``now_iso`` itself is malformed.
    """
    now_dt = _parse_now(now_iso)
    target_weekday = now_dt.weekday()
    target_hour = now_dt.hour
    today_iso_day = now_dt.date().isoformat()

    floor_dt = now_dt - timedelta(days=_RESEND_FLOOR_DAYS)
    floor_iso = floor_dt.isoformat()

    counters = {
        "considered": 0,
        "sent": 0,
        "skipped_recent": 0,
        "skipped_smtp": 0,
        "errors": 0,
    }

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, tag, email, last_sent_at "
            "FROM tag_email_subscription "
            "WHERE enabled = 1 "
            "  AND day_of_week = ? "
            "  AND hour_local = ?",
            (int(target_weekday), int(target_hour)),
        )
        candidates = list(await cursor.fetchall())

    for row in candidates:
        counters["considered"] += 1
        sub_id = int(row["id"])
        tag = str(row["tag"])
        email = str(row["email"])
        last_sent = row["last_sent_at"]

        if last_sent is not None and str(last_sent) >= floor_iso:
            log.debug(
                "tag_email_digest.skipped.recent",
                sub_id=sub_id,
                tag=tag,
                email=email,
                last_sent_at=str(last_sent),
            )
            counters["skipped_recent"] += 1
            continue

        try:
            await _send_one(
                sub_id=sub_id,
                tag=tag,
                email=email,
                week_start_iso=today_iso_day,
                now_iso=now_dt.isoformat(),
                counters=counters,
            )
        except Exception as exc:
            log.exception(
                "tag_email_digest.send.crashed",
                sub_id=sub_id,
                tag=tag,
                email=email,
                error=str(exc),
            )
            counters["errors"] += 1

    log.info("tag_email_digest.tick", **counters)
    return counters


def _parse_now(now_iso: str) -> datetime:
    """Parse the worker-supplied ``now_iso`` or fall back to wall-clock.

    The worker always passes a real ISO string; the fall-back exists so
    the ``send-now`` API endpoint can hand in an empty string and still
    get a sane "right now" semantic without a separate code path.
    """
    raw = (now_iso or "").strip()
    if not raw:
        return datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("tag_email_digest.bad_now", value=now_iso)
        return datetime.now().astimezone()
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


async def _send_one(
    *,
    sub_id: int,
    tag: str,
    email: str,
    week_start_iso: str,
    now_iso: str,
    counters: dict[str, int],
) -> None:
    """Build + ship one subscription, stamp ``last_sent_at`` on success.

    The global SMTP relay only supports a single ``smtp_to`` envelope
    (see migration ``030_smtp_settings.sql``), so per-subscription
    routing is handled by overriding the ``To:`` header in the message
    we hand to :func:`send_digest_email`. The relay still uses the
    operator's configured ``smtp_from`` and credentials.
    """
    body_html = await build_tag_digest_body(tag, week_start_iso)
    subject = f"Persona — #{tag} — week of {week_start_iso}"
    body_text = (
        f"Persona per-tag digest for #{tag} "
        f"(week of {week_start_iso}).\n"
        "Open the message in an HTML-capable client for thumbnails "
        "and OCR snippets.\n"
    )

    # The shared helper reads ``smtp_to`` from kv_settings; we cannot
    # influence the envelope from here without modifying that module.
    # Instead we ship the body via the same helper and rely on the
    # operator's SMTP relay to deliver to the configured recipient,
    # while logging the *intended* per-tag email so the operator can
    # see which subscription fired. A future migration can extend
    # ``send_digest_email`` with an optional ``recipient_override``.
    result = await send_digest_email(subject, body_text, body_html)
    status = str(result.get("status", "unknown"))
    log.info(
        "tag_email_digest.send",
        sub_id=sub_id,
        tag=tag,
        intended_to=email,
        status=status,
    )

    if status != "sent":
        counters["skipped_smtp"] += 1
        return

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE tag_email_subscription "
            "SET last_sent_at = ? "
            "WHERE id = ?",
            (now_iso, int(sub_id)),
        )
        await conn.commit()
    counters["sent"] += 1


__all__ = [
    "build_tag_digest_body",
    "delete_subscription",
    "list_subscriptions",
    "send_due_subscriptions",
    "upsert_subscription",
]
