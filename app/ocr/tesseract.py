"""Wrapper around pytesseract — latent until binary is available."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.settings import get_settings


class OCRNotAvailable(RuntimeError):
    """Raised when Tesseract binary is not configured or not found."""


@dataclass(frozen=True, slots=True)
class TesseractProbe:
    available: bool
    binary_path: Path | None
    version: str | None
    error: str | None


def _resolve_binary_path(tesseract_path: Path | None) -> Path | None:
    """Return a usable Tesseract binary path, or None if not found."""
    if tesseract_path is not None and tesseract_path.exists():
        return tesseract_path
    discovered = shutil.which("tesseract")
    if discovered is None:
        return None
    return Path(discovered)


def is_available(tesseract_path: Path | None = None) -> bool:
    """Quick boolean check for Tesseract availability."""
    settings = get_settings()
    candidate = tesseract_path or settings.tesseract_path
    return _resolve_binary_path(candidate) is not None


def probe_tesseract(tesseract_path: Path | None = None) -> TesseractProbe:
    """Return a detailed availability probe for the settings page."""
    settings = get_settings()
    candidate = tesseract_path or settings.tesseract_path
    resolved = _resolve_binary_path(candidate)
    if resolved is None:
        return TesseractProbe(
            available=False,
            binary_path=None,
            version=None,
            error="Tesseract binary not found in PATH or PERSONA_TESSERACT_PATH",
        )

    try:
        import pytesseract  # type: ignore[import-not-found]

        pytesseract.pytesseract.tesseract_cmd = str(resolved)
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:
        return TesseractProbe(
            available=False,
            binary_path=resolved,
            version=None,
            error=f"Tesseract version probe failed: {exc}",
        )

    return TesseractProbe(
        available=True,
        binary_path=resolved,
        version=version,
        error=None,
    )


def extract_text(
    image: Image.Image,
    *,
    langs: str | None = None,
    tesseract_path: Path | None = None,
) -> str:
    """Run Tesseract OCR on the image and return extracted text.

    Raises OCRNotAvailable if Tesseract binary cannot be located.
    """
    settings = get_settings()
    resolved = _resolve_binary_path(tesseract_path or settings.tesseract_path)
    if resolved is None:
        msg = "Tesseract binary not configured (set PERSONA_TESSERACT_PATH in .env)"
        raise OCRNotAvailable(msg)

    try:
        import pytesseract  # type: ignore[import-not-found]
    except ImportError as exc:
        msg = "pytesseract package not installed"
        raise OCRNotAvailable(msg) from exc

    pytesseract.pytesseract.tesseract_cmd = str(resolved)
    use_langs = langs or settings.tesseract_langs
    text = pytesseract.image_to_string(image, lang=use_langs)
    return str(text).strip()
