"""Manually rebalance storage tiers — useful after changing tier_* settings.

Walks every screenshot, decides its tier from its age, demotes thumbnails
where needed. Idempotent. Pinned screenshots are never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.logging_setup import configure_logging, get_logger
from app.workers.retention import _sweep_once

log = get_logger("persona.recompress")


async def _amain(*, passes: int) -> int:
    configure_logging()
    log.info("recompress.start", passes=passes)
    total_warm = 0
    total_cold = 0
    total_bytes = 0
    for i in range(passes):
        stats = await _sweep_once()
        total_warm += stats["warm"]
        total_cold += stats["cold"]
        total_bytes += int(stats.get("bytes_saved", 0))
        log.info("recompress.pass", n=i + 1, stats=stats)
        if not (stats["warm"] or stats["cold"]):
            break
    log.info(
        "recompress.done",
        warm=total_warm,
        cold=total_cold,
        bytes_saved=total_bytes,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manually rebalance Persona storage tiers")
    parser.add_argument(
        "--passes",
        type=int,
        default=5,
        help="Maximum sweep passes (each sweeps up to 500 hot + 1000 warm rows)",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(passes=args.passes))


if __name__ == "__main__":
    sys.exit(main())
