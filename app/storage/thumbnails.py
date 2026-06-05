"""Filesystem thumbnail writer — WebP, resized, dated subfolders.

The encode ``quality`` is resolved (in order of precedence):

1. Explicit ``quality=`` kwarg — callers like :mod:`app.thumb_regen`
   pin a specific value and the kv knob must not override them.
2. ``capture_image_quality`` kv row (see :mod:`app.capture_quality`)
   — the live slider exposed at ``/settings/capture-quality`` so the
   operator can move the bytes/fidelity trade-off without a restart.
3. :attr:`app.settings.config.Settings.thumbnail_quality` (env-loaded).

The kv lookup uses a short stdlib :mod:`sqlite3` reader because
:func:`save_thumbnail` is invoked from synchronous Pillow code on a
worker thread (see ``asyncio.to_thread`` in the capture loop) — the
aiosqlite pool is off-limits there. WAL mode permits the concurrent
read.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from PIL import Image

from app.settings import get_settings


def _read_live_quality() -> int | None:
    """Return the ``capture_image_quality`` kv value, or ``None`` on miss.

    Silent on every failure mode (missing DB, missing row, unparseable
    payload) — the caller falls back to the env-side default. A noisy
    log here would spam every capture iteration.
    """
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?",
                ("capture_image_quality",),
            )
            row = cursor.fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return int(float(str(row[0]).strip()))
    except (TypeError, ValueError):
        return None


def save_thumbnail(
    image: Image.Image,
    captured_at: datetime,
    screenshot_id: int,
    *,
    thumbnails_dir: Path | None = None,
    quality: int | None = None,
    max_width: int | None = None,
) -> Path:
    """Resize and save image as WebP under data/thumbnails/YYYY-MM-DD/<id>.webp."""
    settings = get_settings()
    out_dir = (thumbnails_dir or settings.thumbnails_dir) / captured_at.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{screenshot_id}.webp"

    target_width = max_width or settings.thumbnail_max_width
    target_quality = quality or _read_live_quality() or settings.thumbnail_quality

    resized = _resize_to_width(image, target_width)
    resized.save(out_path, format="WEBP", quality=target_quality, method=6)
    return out_path


def _resize_to_width(image: Image.Image, max_width: int) -> Image.Image:
    """Return a resized copy preserving aspect ratio. No-op if already small."""
    if image.width <= max_width:
        return image
    ratio = max_width / image.width
    new_height = max(1, int(image.height * ratio))
    return image.resize((max_width, new_height), Image.Resampling.LANCZOS)


def delete_thumbnail(path: Path | None) -> bool:
    """Delete a thumbnail file if it exists. Returns True if a file was removed."""
    if path is None:
        return False
    real = Path(path)
    if not real.exists():
        return False
    real.unlink()
    return True


def list_dated_subfolders(thumbnails_dir: Path | None = None) -> list[Path]:
    """Return all YYYY-MM-DD subfolders inside the thumbnails root."""
    settings = get_settings()
    root = thumbnails_dir or settings.thumbnails_dir
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir() and _looks_like_date(p.name)])


def _looks_like_date(name: str) -> bool:
    if len(name) != 10:
        return False
    try:
        datetime.strptime(name, "%Y-%m-%d")
    except ValueError:
        return False
    return True
