"""Filesystem thumbnail writer — WebP, resized, dated subfolders."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image

from app.settings import get_settings


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
    target_quality = quality or settings.thumbnail_quality

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
