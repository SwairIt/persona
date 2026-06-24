"""Serve thumbnail files from outside the static dir, with safe path resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.settings import get_settings

router = APIRouter(tags=["thumbnails"])


@router.get("/thumbs/{relative_path:path}")
async def thumbnail(
    relative_path: str,
    _user: Annotated[SessionRecord, Depends(current_user_required)],
) -> FileResponse:
    """Stream a WebP thumbnail (owner-only). Path validated against the root.

    Тумбы — это приватные скриншоты экрана владельца. Раньше /thumbs/* был в
    публичном allow-list → любой аноним мог перебором id выкачать всю историю
    экрана. Теперь только под сессией. (Публичные шары/дни получат отдельный
    токен-скоупленный роут эскизов — TODO в NIGHT_BUILD_PLAN Ф5.)
    """
    settings = get_settings()
    root = settings.thumbnails_dir
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    return FileResponse(candidate, media_type="image/webp")


def thumbnail_url(thumbnail_path: str | Path | None) -> str | None:
    """Convert a stored thumbnail absolute path into a /thumbs/ URL."""
    if thumbnail_path is None:
        return None
    settings = get_settings()
    path = Path(thumbnail_path).resolve()
    try:
        relative = path.relative_to(settings.thumbnails_dir)
    except ValueError:
        return None
    return "/thumbs/" + relative.as_posix()
