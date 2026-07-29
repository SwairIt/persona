"""Юнит-тесты серверного ядра очереди «Persona LLM Worker» (срез W-A).

Покрываем queue-модуль app.llm.worker_queue:
- enqueue → claim_next (атомарность: второй claim вернёт None пока один pending)
- add_chunk → read_chunks (after_seq)
- finish_job(done) → get_job.status == 'done'
- rotate/validate_worker_token (верный/неверный)
- worker_online до/после touch_worker

Все тесты идут через init_database + get_connection (фикстура ``db`` из
conftest применяет миграцию 203 на свежую tmp-БД). asyncio_mode='auto', поэтому
маркеры @pytest.mark.asyncio не нужны.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

from app.llm import worker_queue


async def test_enqueue_returns_job_id(db) -> None:
    job_id = await worker_queue.enqueue_job(
        0, "chat", "qwen2.5:3b", {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert isinstance(job_id, int) and job_id > 0
    job = await worker_queue.get_job(job_id)
    assert job is not None
    assert job["status"] == "pending"
    assert job["kind"] == "chat"
    assert job["model"] == "qwen2.5:3b"
    assert job["payload"]["messages"][0]["content"] == "hi"


async def test_claim_next_is_atomic(db) -> None:
    """Один pending → первый claim забирает, второй сразу получает None."""
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {"messages": []})

    first = await worker_queue.claim_next("worker-a")
    assert first is not None
    assert first["id"] == job_id
    assert first["kind"] == "chat"

    # Та же задача уже streaming — второй воркер не должен её перехватить.
    second = await worker_queue.claim_next("worker-b")
    assert second is None

    # Задача помечена streaming.
    job = await worker_queue.get_job(job_id)
    assert job is not None
    assert job["status"] == "streaming"
    assert job["worker_id"] == "worker-a"


async def test_claim_next_empty_queue(db) -> None:
    assert await worker_queue.claim_next("worker-x") is None


async def test_empty_claim_does_not_open_write_transaction(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hot empty-queue path must remain read-only between maintenance ticks."""
    worker_queue._maintenance.last_run = time.monotonic()

    @asynccontextmanager
    async def unexpected_write(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("empty claim unexpectedly acquired a write lock")
        yield

    monkeypatch.setattr(worker_queue, "write_transaction", unexpected_write)
    assert await worker_queue.claim_next("worker-x") is None


async def test_concurrent_claims_with_same_worker_id_never_duplicate(db) -> None:
    """Overlapping long-polls may share worker_id but never return one job twice."""
    first_id = await worker_queue.enqueue_job(0, "chat", "m", {"n": 1})
    second_id = await worker_queue.enqueue_job(0, "chat", "m", {"n": 2})

    claimed = await asyncio.gather(
        worker_queue.claim_next("same-worker"),
        worker_queue.claim_next("same-worker"),
    )
    claimed_ids = [item["id"] for item in claimed if item is not None]
    assert len(claimed_ids) == len(set(claimed_ids))

    # A racing loser is allowed to return None; its next poll gets the remainder.
    while len(claimed_ids) < 2:
        item = await worker_queue.claim_next("same-worker")
        assert item is not None
        claimed_ids.append(item["id"])
    assert set(claimed_ids) == {first_id, second_id}


async def test_chunks_roundtrip_with_after_seq(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {"messages": []})
    await worker_queue.claim_next("worker-a")

    await worker_queue.add_chunk(job_id, 1, "Hel")
    await worker_queue.add_chunk(job_id, 2, "lo ")
    await worker_queue.add_chunk(job_id, 3, "world")

    all_chunks = await worker_queue.read_chunks(job_id, 0)
    assert [c["seq"] for c in all_chunks] == [1, 2, 3]
    assert "".join(c["content"] for c in all_chunks) == "Hello world"

    # after_seq фильтрует уже прочитанные чанки.
    tail = await worker_queue.read_chunks(job_id, 2)
    assert [c["seq"] for c in tail] == [3]
    assert tail[0]["content"] == "world"

    # Ничего нового после последнего seq.
    assert await worker_queue.read_chunks(job_id, 3) == []


async def test_finish_job_done(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {"messages": []})
    await worker_queue.claim_next("worker-a")
    await worker_queue.finish_job(job_id)
    job = await worker_queue.get_job(job_id)
    assert job is not None
    assert job["status"] == "done"
    assert job["error"] is None
    assert job["finished_at"] is not None


async def test_active_generation_and_finish_refresh_worker_heartbeat(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {"messages": []})
    await worker_queue.claim_next("worker-a")
    await db.execute(
        """
        INSERT INTO kv_settings(key, value, updated_at)
        VALUES('llm_worker_last_seen', '2000-01-01T00:00:00+00:00', datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """
    )
    await db.commit()
    assert await worker_queue.worker_online() is False

    await worker_queue.add_chunk(job_id, 0, "working")
    assert await worker_queue.worker_online() is True

    await db.execute(
        "UPDATE kv_settings SET value='2000-01-01T00:00:00+00:00' "
        "WHERE key='llm_worker_last_seen'"
    )
    await db.commit()
    await worker_queue.finish_job(job_id)
    assert await worker_queue.worker_online() is True


async def test_finish_job_error(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {"messages": []})
    await worker_queue.claim_next("worker-a")
    await worker_queue.finish_job(job_id, error="ollama упал")
    job = await worker_queue.get_job(job_id)
    assert job is not None
    assert job["status"] == "error"
    assert job["error"] == "ollama упал"


async def test_finish_job_embed_result_vector(db) -> None:
    """embed-задача завершается с JSON-вектором в result."""
    job_id = await worker_queue.enqueue_job(0, "embed", "nomic-embed-text", {"prompt": "x"})
    await worker_queue.claim_next("worker-a")
    vector = [0.1, 0.2, 0.3]
    await worker_queue.finish_job(job_id, result=json.dumps(vector))
    job = await worker_queue.get_job(job_id)
    assert job is not None
    assert job["status"] == "done"
    assert json.loads(job["result"]) == vector


async def test_read_job_update_returns_chunks_and_status_together(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {})
    await worker_queue.claim_next("worker-a")
    await worker_queue.add_chunk(job_id, 0, "hello")

    chunks, job = await worker_queue.read_job_update(job_id, -1)
    assert chunks == [{"seq": 0, "content": "hello"}]
    assert job is not None
    assert job["status"] == "streaming"


async def test_job_update_event_wakes_without_polling(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {})
    await worker_queue.claim_next("worker-a")
    await worker_queue.read_job_update(job_id, -1)  # clears the enqueue signal

    waiter = asyncio.create_task(worker_queue.wait_for_job_update(job_id, 1.0))
    await asyncio.sleep(0)
    await worker_queue.add_chunk(job_id, 0, "x")
    assert await waiter is True
    worker_queue.forget_job_update(job_id)


async def test_maintenance_fails_stale_job_and_rejects_late_worker(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {})
    await worker_queue.claim_next("worker-a")
    await db.execute(
        "UPDATE llm_job SET claimed_at=datetime('now', '-1 hour') WHERE id=?",
        (job_id,),
    )
    await db.commit()

    result = await worker_queue.maintain_jobs(
        stale_after_seconds=1,
        retention_seconds=24 * 60 * 60,
    )
    assert result["stale_failed"] == 1
    job = await worker_queue.get_job(job_id)
    assert job is not None
    assert job["status"] == "error"
    assert "lease" in job["error"]

    with pytest.raises(worker_queue.WorkerJobStateError):
        await worker_queue.add_chunk(job_id, 1, "late")
    with pytest.raises(worker_queue.WorkerJobStateError):
        await worker_queue.finish_job(job_id)


async def test_maintenance_fails_abandoned_pending_job(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "embed", "m", {})
    await db.execute(
        "UPDATE llm_job SET created_at=datetime('now', '-1 hour') WHERE id=?",
        (job_id,),
    )
    await db.commit()

    result = await worker_queue.maintain_jobs(
        pending_after_seconds=1,
        retention_seconds=24 * 60 * 60,
    )
    assert result["pending_failed"] == 1
    job = await worker_queue.get_job(job_id)
    assert job is not None
    assert job["status"] == "error"


async def test_maintenance_deletes_expired_job_and_chunks(db) -> None:
    job_id = await worker_queue.enqueue_job(0, "chat", "m", {})
    await worker_queue.claim_next("worker-a")
    await worker_queue.add_chunk(job_id, 0, "old")
    await worker_queue.finish_job(job_id)
    await db.execute(
        "UPDATE llm_job SET finished_at=datetime('now', '-1 hour') WHERE id=?",
        (job_id,),
    )
    await db.commit()

    result = await worker_queue.maintain_jobs(
        stale_after_seconds=15 * 60,
        retention_seconds=1,
    )
    assert result["expired_deleted"] == 1
    assert await worker_queue.get_job(job_id) is None
    assert await worker_queue.read_chunks(job_id, -1) == []


async def test_rotate_and_validate_token(db) -> None:
    token = await worker_queue.rotate_worker_token()
    assert isinstance(token, str) and len(token) >= 32

    # Верный токен проходит, неверный — нет.
    assert await worker_queue.validate_worker_token(token) is True
    assert await worker_queue.validate_worker_token("totally-wrong") is False
    assert await worker_queue.validate_worker_token("") is False


async def test_rotate_invalidates_previous_token(db) -> None:
    old = await worker_queue.rotate_worker_token()
    new = await worker_queue.rotate_worker_token()
    assert old != new
    assert await worker_queue.validate_worker_token(new) is True
    # Старый токен после ротации больше не валиден.
    assert await worker_queue.validate_worker_token(old) is False


async def test_validate_without_token_set(db) -> None:
    """Пока токен не ротировали — любой токен невалиден (хэша нет)."""
    assert await worker_queue.validate_worker_token("anything") is False


async def test_worker_online_before_and_after_touch(db) -> None:
    # До любого пинга — офлайн.
    assert await worker_queue.worker_online() is False
    status = await worker_queue.worker_status()
    assert status["online"] is False
    assert status["last_seen"] is None

    # После touch_worker — онлайн, модель видна.
    await worker_queue.touch_worker("host-1", "qwen2.5:3b")
    assert await worker_queue.worker_online() is True
    status = await worker_queue.worker_status()
    assert status["online"] is True
    assert status["model"] == "qwen2.5:3b"
    assert status["last_seen"] is not None


async def test_worker_online_window_allows_slow_five_minute_cycle(db) -> None:
    await worker_queue.touch_worker("slow-worker", "gemma3:4b")
    await db.execute(
        "UPDATE kv_settings SET value=datetime('now', '-4 minutes') "
        "WHERE key='llm_worker_last_seen'"
    )
    await db.commit()
    assert await worker_queue.worker_online() is True

    await db.execute(
        "UPDATE kv_settings SET value=datetime('now', '-6 minutes') "
        "WHERE key='llm_worker_last_seen'"
    )
    await db.commit()
    assert await worker_queue.worker_online() is False
