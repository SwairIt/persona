"""Optional OCR pipeline — activates when Tesseract binary is configured."""

from app.ocr.redaction import redact
from app.ocr.tesseract import (
    OCRNotAvailable,
    extract_text,
    is_available,
    probe_tesseract,
)

__all__ = [
    "OCRNotAvailable",
    "extract_text",
    "is_available",
    "probe_tesseract",
    "redact",
]
