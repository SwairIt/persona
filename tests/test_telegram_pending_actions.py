from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.integrations.telegram.pending_actions import PendingActionStore
from app.storage.db import write_transaction


async def _park(store: PendingActionStore, **overrides: object) -> int:
    defaults: dict[str, object] = {
        "persona_user_id": 42,
        "tool_name": "run_shell",
        "args": {"command": "echo hi"},
        "chat_id": 100,
    }
    defaults.update(overrides)
    return await store.park(**defaults)  # type: ignore[arg-type]


async def test_park_then_claim_returns_the_exact_parked_args(db) -> None:
    del db
    store = PendingActionStore()
    pending_id = await _park(store, args={"command": "rm -rf /tmp/x", "cwd": "/tmp"})

    claimed = await store.claim(42, pending_id, now=datetime.now(UTC))

    assert claimed is not None
    assert claimed["tool_name"] == "run_shell"
    assert claimed["args"] == {"command": "rm -rf /tmp/x", "cwd": "/tmp"}
    assert claimed["persona_user_id"] == 42
    assert claimed["telegram_chat_id"] == 100


async def test_second_claim_of_the_same_id_returns_none(db) -> None:
    del db
    store = PendingActionStore()
    pending_id = await _park(store)

    first = await store.claim(42, pending_id, now=datetime.now(UTC))
    second = await store.claim(42, pending_id, now=datetime.now(UTC))

    assert first is not None
    assert second is None


async def test_claim_after_expiry_returns_none(db) -> None:
    del db
    store = PendingActionStore()
    pending_id = await _park(store)

    # Force this row into the past without waiting out the real TTL.
    async with write_transaction() as conn:
        await conn.execute(
            "UPDATE telegram_pending_action SET expires_at = datetime('now', '-1 minute') "
            "WHERE id = ?",
            (pending_id,),
        )

    claimed = await store.claim(42, pending_id, now=datetime.now(UTC))

    assert claimed is None


async def test_claim_by_a_different_persona_user_id_returns_none(db) -> None:
    del db
    store = PendingActionStore()
    pending_id = await _park(store, persona_user_id=42)

    claimed = await store.claim(999, pending_id, now=datetime.now(UTC))

    assert claimed is None


async def test_unknown_pending_id_returns_none(db) -> None:
    del db
    store = PendingActionStore()

    assert await store.claim(42, 999_999, now=datetime.now(UTC)) is None


async def test_two_concurrent_claims_yield_exactly_one_non_none_result(db) -> None:
    """The single-execution property is the entire reason this table exists:
    a read-then-write claim would let two callbacks both observe
    "not consumed" and both execute. Prove the atomic conditional UPDATE
    actually prevents that under real concurrency.
    """
    del db
    store = PendingActionStore()
    pending_id = await _park(store)

    results = await asyncio.gather(
        store.claim(42, pending_id, now=datetime.now(UTC)),
        store.claim(42, pending_id, now=datetime.now(UTC)),
    )

    non_none = [result for result in results if result is not None]
    assert len(non_none) == 1
