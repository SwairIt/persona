"""Contract tests for profile-aware background lifecycle ownership."""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from app.bootstrap import lifespan as lifespan_module
from app.bootstrap.worker_registry import (
    WORKER_REGISTRY,
    BackgroundRuntime,
    RuntimeProfile,
    WorkerSpec,
    profile_from_environment,
    validate_registry,
    workers_for_profile,
)

_LEGACY_FULL_TASK_NAMES = (
    "runtime-metrics",
    "capture-loop",
    "ocr-worker",
    "retention-worker",
    "embeddings-worker",
    "digest-scheduler",
    "weekly-digest-scheduler",
    "clipboard-worker",
    "inbox-worker",
    "daily-email-scheduler",
    "saved-search-alert",
    "weekly-stats-email",
    "webhook-retry",
    "monthly-digest-scheduler",
    "day-end-summary",
    "auto-backup",
    "audio-worker",
    "audio-retention",
    "hourly-card-worker",
    "daily-pin-worker",
    "card-enrichment-worker",
    "tag-rule-worker",
    "weekly-card-worker",
    "auto-translate-worker",
    "alt-text-worker",
    "auto-pin-worker",
    "entity-extractor-worker",
    "obsidian-sync-worker",
    "daily-pin-enrichment-worker",
    "long-read-worker",
    "s3-sync-worker",
    "weekly-rollup-worker",
    "audio-merge-worker",
    "capture-session-worker",
    "app-budget-worker",
    "ai-reminders-worker",
    "audit-log-rotation-worker",
    "url-time-worker",
    "smart-dedup-worker",
    "email-weekly-digest-worker",
    "memory-of-day-worker",
    "dream-worker",
    "memory-projection-worker",
    "telegram-worker",
    "telegram-pinned-ingest",
    "autowake-dispatcher",
    "persona-impulse-producer",
    "briefing-worker",
    "heartbeat-alert-worker",
    "db-integrity-worker",
    "audio-waveform-worker",
    "transcribe-backfill-worker",
    "smart-pin-worker",
    "tag-email-digest-worker",
    "webhook-csv-worker",
    "ocr-code-detector-worker",
    "sync-apply-worker",
    "storage-cleanup-worker",
)


def _test_spec(name: str) -> WorkerSpec:
    return WorkerSpec(
        name=name,
        module="tests.fake_worker",
        callable_name="run",
        profiles=frozenset({RuntimeProfile.FULL}),
    )


def test_full_profile_preserves_legacy_worker_names_and_order() -> None:
    selected = workers_for_profile(RuntimeProfile.FULL)

    assert tuple(spec.name for spec in selected) == _LEGACY_FULL_TASK_NAMES


def test_lean_profile_starts_only_agent_critical_workers() -> None:
    selected = workers_for_profile(RuntimeProfile.LEAN)

    assert tuple(spec.name for spec in selected) == (
        "runtime-metrics",
        "dream-worker",
        "memory-projection-worker",
        "telegram-worker",
        "telegram-pinned-ingest",
        "autowake-dispatcher",
        "persona-impulse-producer",
    )
    assert profile_from_environment({"PERSONA_LEAN_MODE": "1"}) is RuntimeProfile.LEAN
    assert profile_from_environment({}) is RuntimeProfile.LEAN
    assert profile_from_environment(
        {"PERSONA_RUNTIME_PROFILE": "full"}
    ) is RuntimeProfile.FULL
    assert profile_from_environment({"PERSONA_LEAN_MODE": "0"}) is RuntimeProfile.FULL
    with pytest.raises(ValueError, match="PERSONA_LEAN_MODE"):
        profile_from_environment({"PERSONA_LEAN_MODE": "true"})
    with pytest.raises(ValueError, match="PERSONA_RUNTIME_PROFILE"):
        profile_from_environment({"PERSONA_RUNTIME_PROFILE": "everything"})


def test_registry_has_no_duplicate_worker_names() -> None:
    validate_registry(WORKER_REGISTRY)
    names = [spec.name for spec in WORKER_REGISTRY]

    assert len(names) == len(set(names))


def test_registry_rejects_duplicate_worker_names() -> None:
    duplicate = _test_spec("same-name")

    with pytest.raises(ValueError, match="duplicate worker task names: same-name"):
        validate_registry((duplicate, duplicate))


def test_workers_package_keeps_legacy_api_lazy() -> None:
    script = """
import sys
import app.workers as workers
assert not any(
    name.startswith("app.workers.") for name in sys.modules
)
_ = workers.get_controller
assert "app.workers.control" in sys.modules
assert "app.workers.ocr_worker" not in sys.modules
_ = workers.run_capture_loop
assert "app.workers.capture_loop" in sys.modules
assert "app.workers.ocr_worker" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_every_registered_worker_target_can_be_loaded() -> None:
    for spec in WORKER_REGISTRY:
        target = spec.load_target()
        assert inspect.iscoroutinefunction(target), spec.name


async def test_runtime_preserves_task_names_and_waits_for_cleanup() -> None:
    specs = (_test_spec("first-worker"), _test_spec("second-worker"))
    started = {spec.name: asyncio.Event() for spec in specs}
    cleaned: set[str] = set()
    seen_controller: list[object] = []
    controller = object()

    async def runner(spec: WorkerSpec, received_controller: object) -> None:
        seen_controller.append(received_controller)
        started[spec.name].set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.add(spec.name)

    runtime = BackgroundRuntime(specs, controller, runner=runner)
    tasks = runtime.start()
    await asyncio.gather(*(event.wait() for event in started.values()))

    assert runtime.task_names == ("first-worker", "second-worker")

    await runtime.stop()

    assert seen_controller == [controller, controller]
    assert cleaned == {"first-worker", "second-worker"}
    assert all(task.done() and task.cancelled() for task in tasks)


async def test_runtime_restarts_failed_worker_and_counts_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _test_spec("flaky-worker")
    attempts = 0
    restarted = asyncio.Event()

    async def no_delay(seconds: float) -> None:
        pass

    async def runner(received: WorkerSpec, controller: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient failure")
        restarted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    runtime = BackgroundRuntime((spec,), object(), runner=runner)
    runtime.start()
    await restarted.wait()

    assert runtime.failure_counts == {"flaky-worker": 1}

    await runtime.stop()


async def test_runtime_retains_tasks_when_later_task_creation_fails() -> None:
    specs = (_test_spec("owned-worker"), _test_spec("failed-worker"))
    created: list[asyncio.Task[None]] = []

    async def runner(spec: WorkerSpec, controller: object) -> None:
        await asyncio.Event().wait()

    def task_factory(
        coroutine,  # type: ignore[no-untyped-def]
        *,
        name: str | None = None,
    ) -> asyncio.Task[None]:
        if name == "failed-worker":
            raise RuntimeError("task factory failed")
        task = asyncio.create_task(coroutine, name=name)
        created.append(task)
        return task

    runtime = BackgroundRuntime(
        specs,
        object(),
        runner=runner,
        task_factory=task_factory,
    )

    with pytest.raises(RuntimeError, match="task factory failed"):
        runtime.start()
    await runtime.stop()

    assert runtime.tasks == tuple(created)
    assert all(task.done() and task.cancelled() for task in created)


async def test_runtime_stop_is_bounded_for_cancellation_resistant_worker() -> None:
    spec = WorkerSpec(
        name="stubborn-worker",
        module="tests.fake_worker",
        callable_name="run",
        profiles=frozenset({RuntimeProfile.FULL}),
        stop_timeout_seconds=0.01,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(received: WorkerSpec, controller: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    runtime = BackgroundRuntime((spec,), object(), runner=runner)
    tasks = runtime.start()
    await started.wait()

    await asyncio.wait_for(runtime.stop(), timeout=0.2)

    assert not tasks[0].done()
    release.set()
    await tasks[0]


async def test_lean_lifespan_selects_before_start_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    selected_names: list[str] = []

    class FakeController:
        def pause(self) -> None:
            events.append("pause")

        def request_stop(self) -> None:
            events.append("request-stop")

    controller = FakeController()

    async def initialize() -> None:
        events.append("initialize")

    async def apply_pause(received: object) -> None:
        assert received is controller
        events.append("apply-pause")

    async def shutdown_resources() -> None:
        events.append("shutdown-resources")

    class FakeRuntime:
        def __init__(self, specs: object, received: object) -> None:
            assert received is controller
            selected_names.extend(spec.name for spec in specs)  # type: ignore[attr-defined]

        def start(self) -> tuple[()]:
            events.append("start")
            return ()

        def preflight(self) -> None:
            events.append("preflight")

        async def stop(self) -> None:
            events.append("stop")

    monkeypatch.setenv("PERSONA_LEAN_MODE", "1")
    monkeypatch.setattr(lifespan_module, "_initialize_database", initialize)
    monkeypatch.setattr(lifespan_module, "_get_controller", lambda: controller)
    monkeypatch.setattr(lifespan_module, "_apply_pause_on_boot", apply_pause)
    monkeypatch.setattr(lifespan_module, "_shutdown_automation_resources", shutdown_resources)
    monkeypatch.setattr(lifespan_module, "BackgroundRuntime", FakeRuntime)

    app = FastAPI()
    async with lifespan_module.lifespan(app):
        events.append("serving")
        assert app.state.runtime_profile == "lean"

    assert selected_names == [
        "runtime-metrics",
        "dream-worker",
        "memory-projection-worker",
        "telegram-worker",
        "telegram-pinned-ingest",
        "autowake-dispatcher",
        "persona-impulse-producer",
    ]
    assert events == [
        "initialize",
        "preflight",
        "apply-pause",
        "start",
        "serving",
        "request-stop",
        "stop",
        "shutdown-resources",
    ]


async def test_lifespan_still_stops_runtime_when_controller_shutdown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FailingController:
        def pause(self) -> None:
            pass

        def request_stop(self) -> None:
            events.append("request-stop")
            raise RuntimeError("controller failed")

    class FakeRuntime:
        def __init__(self, specs: object, controller: object) -> None:
            pass

        def start(self) -> tuple[()]:
            return ()

        def preflight(self) -> None:
            pass

        async def stop(self) -> None:
            events.append("stop")

    async def noop() -> None:
        pass

    async def shutdown_resources() -> None:
        events.append("shutdown-resources")

    monkeypatch.setattr(lifespan_module, "_initialize_database", noop)
    monkeypatch.setattr(lifespan_module, "_get_controller", FailingController)
    monkeypatch.setattr(lifespan_module, "_apply_pause_on_boot", lambda controller: noop())
    monkeypatch.setattr(lifespan_module, "_shutdown_automation_resources", shutdown_resources)
    monkeypatch.setattr(lifespan_module, "BackgroundRuntime", FakeRuntime)

    async with lifespan_module.lifespan(FastAPI()):
        pass

    assert events == ["request-stop", "stop", "shutdown-resources"]


def test_create_app_smoke_does_not_start_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web import main  # noqa: PLC0415

    entered = False

    @asynccontextmanager
    async def sentinel_lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        nonlocal entered
        entered = True
        yield

    monkeypatch.setattr(main, "bootstrap_lifespan", sentinel_lifespan)

    app = main.create_app()

    assert app.title == "Persona"
    assert len(app.routes) > 1000
    assert entered is False
    assert not hasattr(app.state, "background_runtime")
