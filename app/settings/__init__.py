"""Application settings loaded from .env via pydantic-settings."""

from app.settings.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
