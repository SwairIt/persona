"""Sanity-check the runtime environment before first launch."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from app.ocr import probe_tesseract
from app.settings import get_settings


def main() -> int:
    settings = get_settings()
    ok = True

    print("Persona environment check")
    print("=" * 40)
    print(f"Python:         {sys.version.split()[0]}")
    print(f"Platform:       {sys.platform}")
    print(f"Data dir:       {settings.data_dir}")
    print(f"DB path:        {settings.db_path}")
    print(f"Thumbnails dir: {settings.thumbnails_dir}")
    print()

    if not settings.data_dir.exists():
        print(f"  [!] data dir does not exist: {settings.data_dir}")
        ok = False
    else:
        print(f"  [ok] data dir exists")

    if not settings.thumbnails_dir.exists():
        print(f"  [!] thumbnails dir does not exist")
        ok = False
    else:
        print(f"  [ok] thumbnails dir exists")

    probe = probe_tesseract(settings.tesseract_path)
    if probe.available:
        print(f"  [ok] Tesseract: {probe.version} at {probe.binary_path}")
    else:
        print(f"  [warn] Tesseract not available — OCR will be skipped")
        print(f"         {probe.error}")

    for module in ("mss", "PIL", "imagehash", "pytesseract", "fastapi", "uvicorn", "aiosqlite"):
        try:
            __import__(module)
            print(f"  [ok] {module} importable")
        except ImportError as exc:
            print(f"  [!] {module} NOT installed: {exc}")
            ok = False

    if not _check_sqlite_fts5():
        print("  [!] SQLite FTS5 module not available — search will not work")
        ok = False
    else:
        print("  [ok] SQLite FTS5 available")

    if _is_safe_to_run_capture():
        print("  [ok] capture path looks safe (writeable)")
    else:
        print("  [warn] capture path looks read-only or strange")

    if ok:
        print("\nAll core checks passed.")
        return 0
    print("\nSome checks failed — review the [!] lines above.")
    return 1


def _check_sqlite_fts5() -> bool:
    import sqlite3

    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
        conn.close()
    except sqlite3.OperationalError:
        return False
    return True


def _is_safe_to_run_capture() -> bool:
    settings = get_settings()
    probe = settings.thumbnails_dir / ".probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
