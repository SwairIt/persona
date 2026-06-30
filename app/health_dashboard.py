"""Consolidated /health-dashboard data builder.

Assembles a single :class:`HealthState` dict from the live SQLite
database and a few in-memory singletons (the capture
:class:`~app.workers.control.CaptureController`, the budget cache).

The shape is deliberately a plain ``dict`` — the HTML template, the
JSON probe and the HTMX fragment all serialise it as-is, so the dict
*is* the public contract.

Every SQL statement is parametrised. Every section is wrapped in a
defensive try/except so a half-migrated database (missing
``audio_segment``, missing ``audit_log``) still renders the page with
neutral defaults rather than raising. The dashboard is a debugging
surface — it MUST render even when something else is broken.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.time import iso, parse_iso
from app.workers.control import get_controller
from app.workers.heartbeat import get_all as get_all_heartbeats

log = get_logger("persona.health_dashboard")


# Worker freshness thresholds (seconds since last heartbeat).
GREEN_THRESHOLD_SECONDS: Final[float] = 120.0
AMBER_THRESHOLD_SECONDS: Final[float] = 600.0

# How many tail rows from each list-style section to surface.
_RECENT_AUDIT_LIMIT: Final[int] = 10
_FEATURE_FLAG_LIMIT: Final[int] = 10


class WorkerStatus(TypedDict):
    """One worker's heartbeat row enriched with a traffic-light label."""

    name: str
    last_heartbeat_iso: str
    seconds_since: float
    status: str  # "green" | "yellow" | "red"
    last_status: str | None
    ticks: int


class DbStats(TypedDict):
    """Top-level row counts + on-disk SQLite size."""

    total_screenshots: int
    total_audio_segments: int
    total_notes: int
    db_size_bytes: int


class BudgetState(TypedDict):
    """Today's storage-budget snapshot for the dashboard tile."""

    today_bytes_used: int
    daily_budget_mb_cap: float
    throttle_level: int
    percent_used: float


class AuditRow(TypedDict):
    """One recent audit_log row, trimmed to display columns."""

    id: int
    ts: str
    action: str
    actor: str | None
    target: str | None
    detail: str | None
    success: bool


class CaptureStatus(TypedDict):
    """Live capture-loop state read from the controller singleton."""

    state: str  # "paused" | "running"
    last_shot_seconds_ago: float | None
    last_shot_iso: str | None
    captures_total: int
    captures_failed: int


class FeatureFlag(TypedDict):
    """One ``*_enabled`` row from ``kv_settings``."""

    key: str
    value: str


class SystemStats(TypedDict):
    """Снимок нагрузки ПК из :mod:`app.system_metrics` (слайс S1).

    ``available`` = False, когда сбор не удался (psutil не установлен,
    модуль S1 ещё не подъехал, и т. п.) — плитка тогда показывает «н/д»
    вместо нулей, которые легко спутать с реальной нулевой нагрузкой.
    ``top_consumer`` — короткая подпись топ-процесса (имя или ``None``).
    """

    available: bool
    cpu_percent: float
    memory_percent: float
    disk_usage_pct: float
    top_consumer: str | None


class HealthState(TypedDict):
    """Wire shape returned by :func:`build_health_state`."""

    now_iso: str
    workers: list[WorkerStatus]
    workers_summary: dict[str, int]
    db_stats: DbStats
    budget: BudgetState
    recent_audit_actions: list[AuditRow]
    capture_status: CaptureStatus
    feature_flags: list[FeatureFlag]
    system: SystemStats


def _status_for(seconds_since: float) -> str:
    """Map ``seconds_since`` to a traffic-light label."""
    if seconds_since < 0:
        return "red"
    if seconds_since < GREEN_THRESHOLD_SECONDS:
        return "green"
    if seconds_since < AMBER_THRESHOLD_SECONDS:
        return "yellow"
    return "red"


async def _collect_workers() -> tuple[list[WorkerStatus], dict[str, int]]:
    """Read every worker heartbeat and tag it with a traffic-light status."""
    rows = await get_all_heartbeats()
    decorated: list[WorkerStatus] = []
    for row in rows:
        status = _status_for(row["seconds_since"])
        decorated.append(
            WorkerStatus(
                name=row["name"],
                last_heartbeat_iso=row["last_run_at"],
                seconds_since=row["seconds_since"],
                status=status,
                last_status=row["last_status"],
                ticks=row["ticks"],
            )
        )
    summary = {
        "green": sum(1 for r in decorated if r["status"] == "green"),
        "yellow": sum(1 for r in decorated if r["status"] == "yellow"),
        "red": sum(1 for r in decorated if r["status"] == "red"),
    }
    return decorated, summary


async def _count_one(conn: aiosqlite.Connection, table: str) -> int:
    """Return ``COUNT(*)`` for ``table`` or 0 on any SQLite error.

    The table name is whitelisted by the caller — never user input — so
    inlining it is safe; the value comparison still goes through a
    parametrised execute() with no placeholders.
    """
    try:
        cursor = await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
        row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0
    except aiosqlite.OperationalError as exc:
        log.debug("health_dashboard.count_failed", table=table, error=str(exc))
        return 0


async def _db_size_bytes(conn: aiosqlite.Connection) -> int:
    """Return ``page_count * page_size`` for the open SQLite database."""
    try:
        cur_pc = await conn.execute("PRAGMA page_count")
        row_pc = await cur_pc.fetchone()
        cur_ps = await conn.execute("PRAGMA page_size")
        row_ps = await cur_ps.fetchone()
        if row_pc is None or row_ps is None:
            return 0
        # PRAGMA result rows expose a single unnamed column at index 0.
        page_count = int(row_pc[0])
        page_size = int(row_ps[0])
        return page_count * page_size
    except (aiosqlite.OperationalError, ValueError, TypeError) as exc:
        log.debug("health_dashboard.db_size_failed", error=str(exc))
        return 0


async def _collect_db_stats() -> DbStats:
    """Gather top-level row counts and the on-disk DB size."""
    async with get_connection() as conn:
        total_screenshots = await _count_one(conn, "screenshots")
        total_audio_segments = await _count_one(conn, "audio_segment")
        total_notes = await _count_one(conn, "notes")
        db_size_bytes = await _db_size_bytes(conn)
    return DbStats(
        total_screenshots=total_screenshots,
        total_audio_segments=total_audio_segments,
        total_notes=total_notes,
        db_size_bytes=db_size_bytes,
    )


async def _collect_budget() -> BudgetState:
    """Read today's budget row + cap and compute a percent-used figure."""
    cfg = get_settings()
    cap_mb = float(cfg.daily_budget_mb)
    cap_bytes = int(cap_mb * 1024 * 1024)

    today_bytes_used = 0
    throttle_level = 0
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT thumbnails_bytes, audio_bytes, events_bytes, "
                "ocr_text_bytes, embeddings_bytes, misc_bytes, throttle_level "
                "FROM daily_budget_state WHERE day = DATE('now')"
            )
            row = await cursor.fetchone()
        if row is not None:
            today_bytes_used = (
                int(row["thumbnails_bytes"])
                + int(row["audio_bytes"])
                + int(row["events_bytes"])
                + int(row["ocr_text_bytes"])
                + int(row["embeddings_bytes"])
                + int(row["misc_bytes"])
            )
            throttle_level = int(row["throttle_level"])
    except aiosqlite.OperationalError as exc:
        log.debug("health_dashboard.budget_read_failed", error=str(exc))

    percent_used = 0.0
    if cap_bytes > 0:
        percent_used = round(today_bytes_used / cap_bytes * 100.0, 2)

    return BudgetState(
        today_bytes_used=today_bytes_used,
        daily_budget_mb_cap=cap_mb,
        throttle_level=throttle_level,
        percent_used=percent_used,
    )


async def _collect_recent_audit() -> list[AuditRow]:
    """Return the last ``_RECENT_AUDIT_LIMIT`` rows from ``audit_log``."""
    rows_out: list[AuditRow] = []
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, ts, action, actor, target, detail, success "
                "FROM audit_log ORDER BY id DESC LIMIT ?",
                (_RECENT_AUDIT_LIMIT,),
            )
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        log.debug("health_dashboard.audit_read_failed", error=str(exc))
        return rows_out

    for row in rows:
        actor_raw = row["actor"]
        target_raw = row["target"]
        detail_raw = row["detail"]
        rows_out.append(
            AuditRow(
                id=int(row["id"]),
                ts=str(row["ts"]),
                action=str(row["action"]),
                actor=str(actor_raw) if actor_raw is not None else None,
                target=str(target_raw) if target_raw is not None else None,
                detail=str(detail_raw) if detail_raw is not None else None,
                success=bool(row["success"]),
            )
        )
    return rows_out


def _collect_capture_status() -> CaptureStatus:
    """Read paused/running + last-shot age from the process-wide controller."""
    controller = get_controller()
    last_shot = controller.last_capture_at
    last_shot_iso: str | None = None
    seconds_ago: float | None = None
    if last_shot is not None:
        if last_shot.tzinfo is None:
            last_shot = last_shot.replace(tzinfo=UTC)
        last_shot_iso = iso(last_shot)
        seconds_ago = round(
            max(0.0, (datetime.now(UTC) - last_shot).total_seconds()),
            3,
        )
    return CaptureStatus(
        state="paused" if controller.paused else "running",
        last_shot_seconds_ago=seconds_ago,
        last_shot_iso=last_shot_iso,
        captures_total=int(controller.captures_total),
        captures_failed=int(controller.captures_failed),
    )


async def _collect_feature_flags() -> list[FeatureFlag]:
    """Return up to ``_FEATURE_FLAG_LIMIT`` kv_settings keys ending in ``_enabled``."""
    flags: list[FeatureFlag] = []
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT key, value FROM kv_settings "
                "WHERE key LIKE ? ORDER BY key ASC LIMIT ?",
                ("%_enabled", _FEATURE_FLAG_LIMIT),
            )
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        log.debug("health_dashboard.flags_read_failed", error=str(exc))
        return flags

    for row in rows:
        flags.append(
            FeatureFlag(
                key=str(row["key"]),
                value=str(row["value"]),
            )
        )
    return flags


def _normalize_top_consumer(raw: object) -> str | None:
    """Свести ``top_consumer`` к короткой строке для плитки.

    Слайс S1 может отдать процесс строкой, либо словарём
    (``{"name": ..., "percent": ...}``). Принимаем оба варианта и
    остальное игнорируем — плитка не должна падать из-за формата.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, dict):
        name = raw.get("name") or raw.get("process") or raw.get("cmd")
        if not name:
            return None
        pct = raw.get("percent")
        if pct is None:
            pct = raw.get("cpu_percent")
        try:
            if pct is not None:
                return f"{name} ({float(pct):.0f}%)"
        except (TypeError, ValueError):
            pass
        return str(name)
    return None


def _collect_system_stats() -> SystemStats:
    """Best-effort снимок нагрузки ПК через слайс S1.

    Как и ``db_stats``, всё обёрнуто в try/except: отсутствие
    ``app.system_metrics`` (модуль ещё не подъехал), отсутствие psutil
    или любой сбой сборщика дают ``available=False`` и нейтральные нули
    — дашборд обязан отрисоваться даже при поломке.
    """
    try:
        # Ленивый импорт: S1 — соседний слайс, его может ещё не быть.
        from app.system_metrics import collect_system_metrics  # noqa: PLC0415

        raw = collect_system_metrics()
        data = dict(raw) if isinstance(raw, dict) else {}
        return SystemStats(
            available=True,
            cpu_percent=round(float(data.get("cpu_percent") or 0.0), 1),
            memory_percent=round(float(data.get("memory_percent") or 0.0), 1),
            disk_usage_pct=round(float(data.get("disk_usage_pct") or 0.0), 1),
            top_consumer=_normalize_top_consumer(data.get("top_consumer")),
        )
    except Exception as exc:  # noqa: BLE001 — дашборд не должен падать
        log.debug("health_dashboard.system_stats_failed", error=str(exc))
        return SystemStats(
            available=False,
            cpu_percent=0.0,
            memory_percent=0.0,
            disk_usage_pct=0.0,
            top_consumer=None,
        )


async def build_health_state() -> HealthState:
    """Build the consolidated health-dashboard payload.

    Every section is best-effort: a failure in one (e.g. ``audio_segment``
    missing on a stale install) yields a neutral value rather than
    raising. The dashboard's whole job is to surface trouble, so it MUST
    keep rendering even when something is wrong.
    """
    now_iso = iso(datetime.now(UTC))
    workers, workers_summary = await _collect_workers()
    db_stats = await _collect_db_stats()
    budget = await _collect_budget()
    recent_audit_actions = await _collect_recent_audit()
    capture_status = _collect_capture_status()
    feature_flags = await _collect_feature_flags()
    system = _collect_system_stats()

    state: HealthState = {
        "now_iso": now_iso,
        "workers": workers,
        "workers_summary": workers_summary,
        "db_stats": db_stats,
        "budget": budget,
        "recent_audit_actions": recent_audit_actions,
        "capture_status": capture_status,
        "feature_flags": feature_flags,
        "system": system,
    }
    log.debug(
        "health_dashboard.built",
        workers=len(workers),
        audit_rows=len(recent_audit_actions),
        flags=len(feature_flags),
        throttle=budget["throttle_level"],
    )
    return state


# parse_iso is re-exported so tests can monkeypatch a deterministic
# clock without dragging in the storage layer's import path.
__all__ = [
    "AMBER_THRESHOLD_SECONDS",
    "GREEN_THRESHOLD_SECONDS",
    "AuditRow",
    "BudgetState",
    "CaptureStatus",
    "DbStats",
    "FeatureFlag",
    "HealthState",
    "SystemStats",
    "WorkerStatus",
    "build_health_state",
    "parse_iso",
]
