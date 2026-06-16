"""SQLite-хранилище монитора: история сэмплов + события генерации моделей."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

_DB_PATH = Path(os.environ.get("LOADMON_DB", str(Path(__file__).parent / "loadmon.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS load_sample (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    cpu_pct REAL, ram_pct REAL, ram_used_mb REAL,
    gpu_pct REAL, vram_used_mb REAL, vram_total_mb REAL, gpu_temp_c REAL,
    models_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_load_sample_ts ON load_sample(ts);

-- События генерации, которые присылает Persona (POST /api/load/event).
CREATE TABLE IF NOT EXISTS model_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    provider TEXT,
    prompt_tokens INTEGER, output_tokens INTEGER,
    elapsed_ms INTEGER, tok_per_s REAL,
    vram_used_mb REAL
);
CREATE INDEX IF NOT EXISTS idx_model_event_model ON model_event(model, ts);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def insert_sample(d: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO load_sample(cpu_pct,ram_pct,ram_used_mb,gpu_pct,vram_used_mb,"
            "vram_total_mb,gpu_temp_c,models_json) VALUES(?,?,?,?,?,?,?,?)",
            (d.get("cpu_pct"), d.get("ram_pct"), d.get("ram_used_mb"), d.get("gpu_pct"),
             d.get("vram_used_mb"), d.get("vram_total_mb"), d.get("gpu_temp_c"),
             d.get("models_json")),
        )
        conn.commit()


def recent_samples(minutes: int = 10, limit: int = 600) -> list[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT ts,cpu_pct,ram_pct,ram_used_mb,gpu_pct,vram_used_mb,vram_total_mb,gpu_temp_c "
            "FROM load_sample WHERE ts >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
            (f"-{int(minutes)} minutes", int(limit)),
        )
        rows = [dict(r) for r in cur.fetchall()]
    rows.reverse()
    return rows


def insert_event(d: dict[str, Any]) -> int:
    tok_per_s = d.get("tok_per_s")
    if tok_per_s is None and d.get("output_tokens") and d.get("elapsed_ms"):
        try:
            tok_per_s = round(1000.0 * float(d["output_tokens"]) / float(d["elapsed_ms"]), 2)
        except (ZeroDivisionError, ValueError, TypeError):
            tok_per_s = None
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO model_event(model,provider,prompt_tokens,output_tokens,elapsed_ms,tok_per_s,vram_used_mb) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(d.get("model") or "?"), d.get("provider"), d.get("prompt_tokens"),
             d.get("output_tokens"), d.get("elapsed_ms"), tok_per_s, d.get("vram_used_mb")),
        )
        conn.commit()
        return int(cur.lastrowid)


def model_stats() -> list[dict[str, Any]]:
    """Агрегаты по моделям + простая оптимизационная пометка."""
    with _conn() as conn:
        cur = conn.execute(
            "SELECT model, COUNT(*) n, AVG(tok_per_s) avg_tps, MAX(tok_per_s) max_tps, "
            "AVG(elapsed_ms) avg_ms, AVG(vram_used_mb) avg_vram, MAX(ts) last_ts "
            "FROM model_event GROUP BY model ORDER BY n DESC"
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        tps = r.get("avg_tps") or 0
        # эвристика: <8 ток/с на локалке = вероятно partial offload в RAM (тормозит)
        r["hint"] = (
            "медленно — вероятно модель не влезла в VRAM целиком (partial offload); возьми меньше/квант Q4"
            if 0 < tps < 8 else ("ок" if tps >= 8 else "мало данных")
        )
    return rows


__all__ = ["init_db", "insert_sample", "recent_samples", "insert_event", "model_stats"]
