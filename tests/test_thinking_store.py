from __future__ import annotations

from app.thinking.store import ThoughtStore


async def _user(db, user_id: int = 7) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


async def test_chain_records_seed_steps_and_conclusion_in_order(db) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7,
        seed_text="Что я поняла про владельца сегодня?",
        seed_kind="know_you",
        source_scope="owner_private",
        source_session_id=11,
    )
    assert await store.append_step(chain_id, text="Он не любит длинные ответы.") == 1
    assert await store.append_step(chain_id, text="Значит короче формулировать.") == 2
    await store.close_chain(chain_id, conclusion="Отвечать короче по умолчанию.")

    steps = await store.chain_steps(chain_id)
    assert [s["kind"] for s in steps] == ["seed", "step", "step", "conclusion"]
    assert [s["step_no"] for s in steps] == [0, 1, 2, 3]
    assert steps[-1]["text"] == "Отвечать короче по умолчанию."


async def test_closed_chain_is_not_returned_as_open(db) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    assert (await store.oldest_open_chain(7))["chain_id"] == chain_id
    await store.close_chain(chain_id, conclusion="done")
    assert await store.oldest_open_chain(7) is None


async def test_a_half_finished_chain_stays_resumable(db) -> None:
    """Owner interrupted the loop: the chain must survive with its steps and be
    picked up again next time the owner goes quiet. This is the whole
    preemption mechanism — an interrupted step simply never gets written, and
    the chain is still the oldest open one."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="unfinished",
        source_scope="owner_private", source_session_id=None,
    )
    await store.append_step(chain_id, text="половина мысли")
    assert (await store.oldest_open_chain(7))["chain_id"] == chain_id
    assert len(await store.chain_steps(chain_id)) == 2


async def test_steps_used_today_counts_only_this_tenant(db) -> None:
    await _user(db, 7)
    await _user(db, 8)
    store = ThoughtStore()
    a = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    await store.append_step(a, text="one")
    b = await store.open_chain(
        8, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    await store.append_step(b, text="one")
    await store.append_step(b, text="two")
    assert await store.steps_used_today(7) == 2   # seed + 1 step
    assert await store.steps_used_today(8) == 3   # seed + 2 steps
