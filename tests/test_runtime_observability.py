"""Runtime lag, DB writer wait and queue-depth telemetry contracts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from app.observability import runtime as runtime_metrics
from app.storage.db import write_transaction

if TYPE_CHECKING:
    from pathlib import Path


def test_runtime_snapshot_is_bounded_and_reports_failures() -> None:
    runtime_metrics._reset_for_tests()

    runtime_metrics.record_db_write_wait(0.012, acquired=True)
    runtime_metrics.record_db_write_wait(0.250, acquired=False)
    snapshot = runtime_metrics.runtime_snapshot()
    db_wait = snapshot["db_write_lock_wait"]

    assert isinstance(db_wait, dict)
    assert db_wait["samples"] == 2
    assert db_wait["max_ms"] == 250.0
    assert db_wait["attempts_total"] == 2
    assert db_wait["failures_total"] == 1


def test_busy_writer_history_does_not_collapse_to_four_samples_per_second() -> None:
    runtime_metrics._reset_for_tests()

    for _ in range(5_000):
        runtime_metrics.record_db_write_wait(0.001, acquired=True)

    db_wait = runtime_metrics.runtime_snapshot()["db_write_lock_wait"]
    assert isinstance(db_wait, dict)
    assert db_wait["samples"] == 5_000
    assert db_wait["attempts_total"] == 5_000


@pytest.mark.asyncio
async def test_event_loop_monitor_stops_and_records_sample() -> None:
    runtime_metrics._reset_for_tests()
    stop = asyncio.Event()
    task = asyncio.create_task(
        runtime_metrics.monitor_event_loop(stop, interval_seconds=0.05)
    )

    await asyncio.sleep(0.12)
    stop.set()
    await task

    lag = runtime_metrics.runtime_snapshot()["event_loop_lag"]
    assert isinstance(lag, dict)
    assert int(lag["samples"] or 0) >= 1


@pytest.mark.asyncio
async def test_write_transaction_records_begin_immediate_wait(
    tmp_path: Path,
) -> None:
    runtime_metrics._reset_for_tests()
    db_path = tmp_path / "metrics.db"

    async with write_transaction(db_path) as conn:
        await conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")

    db_wait = runtime_metrics.runtime_snapshot()["db_write_lock_wait"]
    assert isinstance(db_wait, dict)
    assert db_wait["attempts_total"] == 1
    assert db_wait["failures_total"] == 0


@pytest.mark.asyncio
async def test_queue_depths_reports_known_queues(db) -> None:
    await db.execute(
        """
        INSERT INTO llm_job(kind, status)
        VALUES ('chat', 'pending'), ('chat', 'done')
        """
    )
    await db.execute(
        """
        INSERT INTO telegram_update_inbox(
            update_id, status, holder_id, lease_until
        ) VALUES (1, 'processing', 'test-holder', datetime('now', '+1 minute'))
        """
    )
    await db.commit()

    depths = await runtime_metrics.queue_depths()

    assert depths["llm"] == {"pending": 1}
    assert depths["remote_browser"] == {}
    assert depths["autowake"] == {}
    assert depths["telegram_inbox"] == {"processing": 1}
