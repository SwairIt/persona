"""Remove thumbnail files that are no longer referenced by the database.

Also clears stale `thumbnail_path` rows whose target file is missing.

Safe to run while Persona is up — operates atomically per file.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.logging_setup import configure_logging, get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.cleanup")


async def _amain(*, dry_run: bool) -> int:
    configure_logging()
    settings = get_settings()
    settings.ensure_directories()

    referenced: set[Path] = set()
    missing_paths: list[int] = []

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots WHERE thumbnail_path IS NOT NULL"
        )
        rows = await cursor.fetchall()
        for row in rows:
            raw = row["thumbnail_path"]
            if not raw:
                continue
            path = Path(raw).resolve()
            if path.exists():
                referenced.add(path)
            else:
                missing_paths.append(int(row["id"]))

        if not dry_run and missing_paths:
            await conn.executemany(
                "UPDATE screenshots SET thumbnail_path = NULL WHERE id = ?",
                [(sid,) for sid in missing_paths],
            )
            await conn.commit()

    log.info(
        "cleanup.scanned",
        referenced=len(referenced),
        missing_paths=len(missing_paths),
        dry_run=dry_run,
    )

    orphans: list[Path] = []
    if settings.thumbnails_dir.exists():
        for path in settings.thumbnails_dir.rglob("*.webp"):
            resolved = path.resolve()
            if resolved not in referenced:
                orphans.append(resolved)

    removed = 0
    bytes_freed = 0
    for path in orphans:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if dry_run:
            removed += 1
            bytes_freed += size
            continue
        try:
            path.unlink()
            removed += 1
            bytes_freed += size
        except OSError as exc:
            log.warning("cleanup.delete_failed", path=str(path), error=str(exc))

    log.info(
        "cleanup.done",
        orphans_found=len(orphans),
        removed=removed,
        bytes_freed=bytes_freed,
        dry_run=dry_run,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Persona orphan-thumbnail cleanup")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without deleting anything",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
