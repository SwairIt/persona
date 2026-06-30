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

import json

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
