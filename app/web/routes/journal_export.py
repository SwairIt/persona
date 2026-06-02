"""Per-day journal export to Markdown.

Bundles into one .md: today's auto-digest (if any), all notes for the day,
session blocks, and a compact list of significant captures.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.storage.db import get_connection
from app.storage.time import iso, parse_iso

router = APIRouter(prefix="/api/export", tags=["journal-export"])


@router.get("/journal.md")
async def journal_day_markdown(date_str: str | None = Query(default=None, alias="date")) -> Response:
    target = _parse_day(date_str)
    start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT body, provider FROM daily_digest WHERE day = ?",
            (target.isoformat(),),
        )
        digest_row = await cursor.fetchone()

        cursor = await conn.execute(
            """
            SELECT n.screenshot_id, n.body, n.updated_at,
                   s.app_name, s.window_title, s.captured_at
            FROM screenshot_notes n
            JOIN screenshots s ON s.id = n.screenshot_id
            WHERE s.captured_at >= ? AND s.captured_at < ?
            ORDER BY n.updated_at
            """,
            (iso(start), iso(end)),
        )
        notes = [
            {
                "id": int(row["screenshot_id"]),
                "body": str(row["body"]),
                "updated_at": parse_iso(str(row["updated_at"])),
                "app_name": row["app_name"],
                "window_title": row["window_title"],
            }
            for row in await cursor.fetchall()
        ]

        cursor = await conn.execute(
            """
            SELECT id, started_at, ended_at, duration_minutes, intent, outcome, completed
            FROM focus_sessions
            WHERE DATE(started_at) = ?
            ORDER BY started_at
            """,
            (target.isoformat(),),
        )
        focus = [
            {
                "id": int(row["id"]),
                "started_at": parse_iso(str(row["started_at"])),
                "ended_at": parse_iso(str(row["ended_at"])) if row["ended_at"] else None,
                "duration_minutes": int(row["duration_minutes"]),
                "intent": row["intent"],
                "outcome": row["outcome"],
                "completed": bool(row["completed"]),
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
            (str(row["app_name"]), int(row["n"]))
            for row in await cursor.fetchall()
        ]

    if not (digest_row or notes or focus or top_apps):
        raise HTTPException(status_code=404, detail="Nothing to export for this day")

    lines: list[str] = [f"# Journal — {target.isoformat()}", ""]

    if digest_row:
        lines.append("## AI digest")
        if digest_row["provider"]:
            lines.append(f"_via {digest_row['provider']}_")
        lines.append("")
        lines.append(str(digest_row["body"]).strip())
        lines.append("")

    if top_apps:
        lines.append("## Top apps")
        for app, n in top_apps:
            lines.append(f"- **{app}** — {n} captures")
        lines.append("")

    if focus:
        lines.append("## Focus sessions")
        for session in focus:
            ended = session["ended_at"].strftime("%H:%M") if session["ended_at"] else "—"
            mark = "✓" if session["completed"] else "✗"
            label = session["intent"] or "(no intent)"
            lines.append(
                f"- {mark} **{session['started_at'].strftime('%H:%M')}–{ended}** "
                f"({session['duration_minutes']}m) — {label}"
            )
            if session["outcome"]:
                lines.append(f"  > {session['outcome']}")
        lines.append("")

    if notes:
        lines.append("## Notes")
        for note in notes:
            ts = note["updated_at"].strftime("%H:%M")
            heading = note["app_name"] or "—"
            if note["window_title"]:
                heading += f" — {note['window_title']}"
            lines.append(f"### {ts} — {heading}")
            lines.append(note["body"].rstrip())
            lines.append(f"[screenshot #{note['id']}](/screenshot/{note['id']})")
            lines.append("")

    body = "\n".join(lines)
    filename = f"persona-journal-{target.isoformat()}.md"
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date") from exc
