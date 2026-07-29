"""Тесты слайса W-B — провайдер 'worker' (WorkerLLMClient) + embed через очередь.

Контракт: ПК делает ИСХОДЯЩИЕ запросы к серверу, сервер кладёт задачи в очередь
(app.llm.worker_queue), ПК-воркер их забирает и шлёт чанки обратно. Здесь мы
проверяем СЕРВЕРНУЮ сторону: WorkerLLMClient.stream поллит read_chunks/get_job и
yield-ит дельты в том же формате, что OllamaClient.stream (голые строки), а при
worker_online()==False даёт понятную ошибку. И что memory_vec.embed при провайдере
'worker' уходит в очередь (enqueue_job kind='embed'), не трогая HTTP-путь.

Модуль app.llm.worker_queue (слайс W-A) может ещё не существовать на момент
прогона — поэтому в каждом тесте мы кладём ФЕЙКОВЫЙ модуль в sys.modules, чтобы
ленивые импорты внутри функций его подхватили.
"""

from __future__ import annotations

import sys
import types

import pytest

from app.llm.client import WorkerLLMClient, CompletionRequest, LLMNotConfigured


def _install_fake_worker_queue(monkeypatch: pytest.MonkeyPatch, **funcs: object) -> dict:
    """Положить фейковый app.llm.worker_queue в sys.modules.

    ``funcs`` — переопределения (async) для enqueue_job/read_chunks/get_job/
    worker_online. Возвращает словарь записей вызовов для ассертов.
    """
    calls: dict[str, list] = {
        "enqueue": [],
        "read_chunks": [],
        "get_job": [],
        "read_job_update": [],
        "wait_for_job_update": [],
        "forget_job_update": [],
    }
    mod = types.ModuleType("app.llm.worker_queue")

    async def _default_worker_online() -> bool:
        return True

    async def _default_enqueue_job(user_id: int, kind: str, model: str, payload: dict) -> int:
        calls["enqueue"].append({"user_id": user_id, "kind": kind, "model": model,
                                 "payload": payload})
        return 1

    async def _default_read_chunks(job_id: int, after_seq: int) -> list[dict]:
        calls["read_chunks"].append({"job_id": job_id, "after_seq": after_seq})
        return []

    async def _default_get_job(job_id: int) -> dict | None:
        calls["get_job"].append({"job_id": job_id})
        return {"id": job_id, "status": "done", "error": None, "result": None}

    mod.worker_online = funcs.get("worker_online", _default_worker_online)  # type: ignore[attr-defined]
    mod.enqueue_job = funcs.get("enqueue_job", _default_enqueue_job)  # type: ignore[attr-defined]
    mod.read_chunks = funcs.get("read_chunks", _default_read_chunks)  # type: ignore[attr-defined]
    mod.get_job = funcs.get("get_job", _default_get_job)  # type: ignore[attr-defined]
    for optional_name in (
        "read_job_update",
        "wait_for_job_update",
        "forget_job_update",
        "cancel_job",
    ):
        if optional_name in funcs:
            setattr(mod, optional_name, funcs[optional_name])
    monkeypatch.setitem(sys.modules, "app.llm.worker_queue", mod)
    return calls


def _req(user: str = "привет", system: str = "ты ассистент") -> CompletionRequest:
    return CompletionRequest(system=system, user=user, max_tokens=128, temperature=0.4)


# ── stream: дельты по чанкам + завершение на status='done' ──────────────────


async def test_stream_yields_deltas_and_finishes_on_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_chunks отдаёт чанки порциями; get_job в конце говорит 'done'."""
    # Сценарий: 1-й опрос → 2 чанка; 2-й опрос → 1 чанк; затем get_job=done.
    chunk_batches = [
        [{"seq": 1, "content": "При"}, {"seq": 2, "content": "вет"}],
        [{"seq": 3, "content": "!"}],
        [],  # хвост после done (пусто)
    ]
    job_states = [
        {"status": "streaming"},  # после 1-й порции
        {"status": "streaming"},  # после 2-й порции
        {"status": "done", "error": None, "result": None},
    ]
    rc_idx = {"i": 0}
    gj_idx = {"i": 0}

    async def read_chunks(job_id: int, after_seq: int) -> list[dict]:
        i = rc_idx["i"]
        rc_idx["i"] += 1
        return chunk_batches[i] if i < len(chunk_batches) else []

    async def get_job(job_id: int) -> dict:
        i = gj_idx["i"]
        gj_idx["i"] += 1
        return job_states[i] if i < len(job_states) else job_states[-1]

    _install_fake_worker_queue(monkeypatch, read_chunks=read_chunks, get_job=get_job)

    client = WorkerLLMClient(model="qwen2.5:3b")
    out = [delta async for delta in client.stream(_req())]
    assert out == ["При", "вет", "!"]
    assert "".join(out) == "Привет!"


async def test_stream_enqueues_chat_job_with_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream кладёт в очередь задачу kind='chat' с messages из request."""
    calls = _install_fake_worker_queue(monkeypatch)
    client = WorkerLLMClient(model="my-model")
    _ = [d async for d in client.stream(_req(user="вопрос", system="контекст"))]

    assert len(calls["enqueue"]) == 1
    job = calls["enqueue"][0]
    assert job["kind"] == "chat"
    assert job["model"] == "my-model"
    msgs = job["payload"]["messages"]
    assert msgs[0] == {"role": "system", "content": "контекст"}
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "вопрос"
    assert job["payload"]["options"]["num_ctx"] == 2048
    assert job["payload"]["options"]["num_predict"] == 128
    assert job["payload"]["options"]["repeat_penalty"] == 1.15
    assert job["payload"]["options"]["repeat_last_n"] == 256
    assert job["payload"]["keep_alive"] == "30m"
    assert job["payload"]["think"] is False


async def test_stream_preserves_interactive_job_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_worker_queue(monkeypatch)
    client = WorkerLLMClient(model="my-model", job_kind="telegram_conversation")

    _ = [d async for d in client.stream(_req())]

    assert calls["enqueue"][0]["kind"] == "telegram_conversation"
    assert calls["enqueue"][0]["payload"]["delivery"] == "complete"
    assert calls["enqueue"][0]["payload"]["keep_alive"] == "-1"


async def test_telegram_complete_delivery_yields_job_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_job(job_id: int) -> dict:
        return {"status": "done", "result": "готовый ответ"}

    _install_fake_worker_queue(monkeypatch, get_job=get_job)
    client = WorkerLLMClient(job_kind="telegram_conversation")

    out = [delta async for delta in client.stream(_req())]

    assert out == ["готовый ответ"]


async def test_stream_uses_event_driven_combined_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production queue path avoids two DB polls every 40 ms."""
    updates = [
        ([{"seq": 0, "content": "ok"}], {"status": "streaming"}),
        ([], {"status": "done"}),
    ]
    calls: dict[str, int] = {"read": 0, "wait": 0, "forget": 0}

    async def read_job_update(job_id: int, after_seq: int):
        calls["read"] += 1
        return updates.pop(0)

    async def wait_for_job_update(job_id: int, wait_seconds: float) -> bool:
        calls["wait"] += 1
        return True

    def forget_job_update(job_id: int) -> None:
        calls["forget"] += 1

    legacy_calls = _install_fake_worker_queue(
        monkeypatch,
        read_job_update=read_job_update,
        wait_for_job_update=wait_for_job_update,
        forget_job_update=forget_job_update,
    )
    out = [delta async for delta in WorkerLLMClient().stream(_req())]

    assert out == ["ok"]
    assert calls == {"read": 2, "wait": 1, "forget": 1}
    assert legacy_calls["get_job"] == []


# ── worker_online()==False → понятная ошибка ────────────────────────────────


async def test_stream_offline_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def offline() -> bool:
        return False

    _install_fake_worker_queue(monkeypatch, worker_online=offline)
    client = WorkerLLMClient()
    with pytest.raises(LLMNotConfigured) as exc:
        _ = [d async for d in client.stream(_req())]
    assert "офлайн" in str(exc.value).lower() or "офлайн" in str(exc.value)


async def test_stream_error_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """status='error' → исключение с текстом ошибки воркера."""
    async def get_job(job_id: int) -> dict:
        return {"status": "error", "error": "ollama упала"}

    async def read_chunks(job_id: int, after_seq: int) -> list[dict]:
        return []

    _install_fake_worker_queue(monkeypatch, get_job=get_job, read_chunks=read_chunks)
    client = WorkerLLMClient()
    with pytest.raises(LLMNotConfigured) as exc:
        _ = [d async for d in client.stream(_req())]
    assert "ollama упала" in str(exc.value)


async def test_stream_timeout_cancels_durable_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled: list[tuple[int, str]] = []

    async def get_job(job_id: int) -> dict:
        return {"status": "pending"}

    async def cancel_job(job_id: int, reason: str) -> bool:
        cancelled.append((job_id, reason))
        return True

    _install_fake_worker_queue(
        monkeypatch,
        get_job=get_job,
        cancel_job=cancel_job,
    )
    client = WorkerLLMClient()
    client._STALL_TIMEOUT = -1

    with pytest.raises(LLMNotConfigured, match="таймаут"):
        _ = [d async for d in client.stream(_req())]

    assert cancelled == [(1, "request_cancelled_or_timed_out")]


async def test_stream_missing_queue_module_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если app.llm.worker_queue не импортируется — понятная ошибка, не ImportError."""
    # Подсунем модуль, который ломается при доступе к атрибутам импорта: проще
    # положить None, чтобы from ... import ... упал.
    monkeypatch.setitem(sys.modules, "app.llm.worker_queue", None)  # type: ignore[arg-type]
    client = WorkerLLMClient()
    with pytest.raises(LLMNotConfigured):
        _ = [d async for d in client.stream(_req())]


# ── make_client: ветка 'worker' ─────────────────────────────────────────────


def test_make_client_worker_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """make_client(provider='worker') → WorkerLLMClient без ключа."""
    from app.llm import make_client
    from app.llm.client import _UsageRecordingClient

    client = make_client(provider="worker")
    assert isinstance(client, _UsageRecordingClient)
    assert client.provider == "worker"
    assert isinstance(client._inner, WorkerLLMClient)


def test_make_client_worker_passes_job_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm import make_client

    client = make_client(provider="worker", kind="telegram_ambient_reply")

    assert client._inner._job_kind == "telegram_ambient_reply"


# ── memory_vec.embed: провайдер 'worker' → enqueue_job(kind='embed') ────────


async def test_embed_via_worker_enqueues_embed_job(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """При kv llm_provider='worker' embed уходит в очередь (kind='embed'),
    HTTP-путь не вызывается; результат — JSON-вектор из job.result."""
    import json

    from app.storage.repository import set_kv
    from app import memory_vec

    await set_kv(db, "llm_provider", "worker")
    await db.commit()

    captured: dict = {}

    async def enqueue_job(user_id: int, kind: str, model: str, payload: dict) -> int:
        captured["kind"] = kind
        captured["model"] = model
        captured["payload"] = payload
        return 7

    async def get_job(job_id: int) -> dict:
        return {"status": "done", "result": json.dumps([0.1, 0.2, 0.3])}

    _install_fake_worker_queue(monkeypatch, enqueue_job=enqueue_job, get_job=get_job)

    # Страховка: если код по ошибке пойдёт по HTTP-пути — взорвём тест.
    async def _boom_endpoint() -> str:
        raise AssertionError("embed ушёл по HTTP-пути, а должен был в очередь")

    monkeypatch.setattr(memory_vec, "_ollama_endpoint", _boom_endpoint)

    vec = await memory_vec.embed("какой-то текст", kind="document")
    assert vec == [0.1, 0.2, 0.3]
    assert captured["kind"] == "embed"
    assert "prompt" in captured["payload"]


async def test_embed_non_worker_uses_http_path(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """Провайдер НЕ 'worker' → текущий HTTP-путь не сломан (очередь не зовётся)."""
    from app.storage.repository import set_kv
    from app import memory_vec

    await set_kv(db, "llm_provider", "ollama")
    await db.commit()

    async def enqueue_job(user_id: int, kind: str, model: str, payload: dict) -> int:
        raise AssertionError("не должны звать очередь при провайдере != worker")

    _install_fake_worker_queue(monkeypatch, enqueue_job=enqueue_job)

    # HTTP-путь: замокаем endpoint, чтобы не было реальной сети — embed вернёт
    # None (httpx не достучится до фейкового хоста), но enqueue_job НЕ вызовется.
    async def _fake_endpoint() -> str:
        return "http://127.0.0.1:1"  # заведомо мёртвый порт → None, без очереди

    monkeypatch.setattr(memory_vec, "_ollama_endpoint", _fake_endpoint)
    vec = await memory_vec.embed("текст", kind="document")
    assert vec is None  # сеть недоступна → тихий None, но в очередь не ушли
