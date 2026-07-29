"""Declarative background-worker registry and runtime profiles.

Worker modules are referenced by import path instead of being imported here.
This keeps web application import side-effect free and lets a deployment profile
select workers before any disabled worker coroutine is constructed.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

from app.logging_setup import get_logger

if TYPE_CHECKING:
    from types import ModuleType


class RuntimeProfile(StrEnum):
    """Compatibility profiles for the current single-process deployment."""

    FULL = "full"
    LEAN = "lean"


class ResourceClass(StrEnum):
    """Dominant resource used by a worker.

    This is metadata only in the first bootstrap slice. It makes resource
    ownership explicit without changing scheduling or concurrency yet.
    """

    MIXED = "mixed"
    CPU = "cpu"
    IO = "io"
    NETWORK = "network"


class TaskFactory(Protocol):
    """Subset of ``asyncio.create_task`` used by the runtime."""

    def __call__(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[None]: ...


WorkerTarget = Callable[..., Coroutine[Any, Any, None]]
WorkerRunner = Callable[["WorkerSpec", object], Coroutine[Any, Any, None]]


def _create_task(
    coroutine: Coroutine[Any, Any, None],
    *,
    name: str | None = None,
) -> asyncio.Task[None]:
    return asyncio.create_task(coroutine, name=name)


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """One lazily loaded worker owned by the process lifecycle."""

    name: str
    module: str
    callable_name: str
    profiles: frozenset[RuntimeProfile]
    pass_controller: bool = True
    resource_class: ResourceClass = ResourceClass.MIXED
    cadence: str = "worker-defined"
    concurrency_limit: int = 1
    stop_timeout_seconds: float = 10.0
    restart_on_failure: bool = True
    restart_backoff_max_seconds: float = 60.0

    def load_target(
        self,
        *,
        importer: Callable[[str], ModuleType] = importlib.import_module,
    ) -> WorkerTarget:
        """Import and return the worker entry point only when it is selected."""

        module = importer(self.module)
        target = getattr(module, self.callable_name)
        if not callable(target):
            msg = f"{self.module}.{self.callable_name} is not callable"
            raise TypeError(msg)
        return cast("WorkerTarget", target)


_FULL = frozenset({RuntimeProfile.FULL})
_FULL_AND_LEAN = frozenset({RuntimeProfile.FULL, RuntimeProfile.LEAN})


def _spec(
    name: str,
    module: str,
    callable_name: str,
    *,
    pass_controller: bool = True,
    profiles: frozenset[RuntimeProfile] = _FULL,
    resource_class: ResourceClass = ResourceClass.MIXED,
    cadence: str = "worker-defined",
) -> WorkerSpec:
    return WorkerSpec(
        name=name,
        module=module,
        callable_name=callable_name,
        pass_controller=pass_controller,
        profiles=profiles,
        resource_class=resource_class,
        cadence=cadence,
    )


# Order is deliberate and matches the legacy app.web.main lifespan exactly.
# Do not reorder casually: tasks may observe startup state established by an
# earlier worker before its first await.
WORKER_REGISTRY: tuple[WorkerSpec, ...] = (
    _spec(
        "runtime-metrics",
        "app.workers.runtime_metrics_worker",
        "run_runtime_metrics_worker",
        pass_controller=False,
        profiles=_FULL_AND_LEAN,
        resource_class=ResourceClass.IO,
        cadence="1s event-loop lag sample",
    ),
    _spec("capture-loop", "app.workers.capture_loop", "run_capture_loop"),
    _spec(
        "ocr-worker",
        "app.workers.ocr_worker",
        "run_ocr_worker",
        resource_class=ResourceClass.CPU,
    ),
    _spec("retention-worker", "app.workers.retention", "run_retention_worker"),
    _spec("embeddings-worker", "app.workers.embeddings_worker", "run_embeddings_worker"),
    _spec("digest-scheduler", "app.workers.digest_scheduler", "run_digest_scheduler"),
    _spec(
        "weekly-digest-scheduler",
        "app.workers.weekly_digest_scheduler",
        "run_weekly_digest_scheduler",
    ),
    _spec("clipboard-worker", "app.workers.clipboard_worker", "run_clipboard_worker"),
    _spec("inbox-worker", "app.workers.inbox_worker", "run_inbox_worker"),
    _spec(
        "daily-email-scheduler",
        "app.workers.daily_email_scheduler",
        "run_daily_email_scheduler",
        resource_class=ResourceClass.NETWORK,
    ),
    _spec(
        "saved-search-alert",
        "app.workers.saved_search_alert",
        "run_saved_search_alert_worker",
    ),
    _spec(
        "weekly-stats-email",
        "app.workers.weekly_stats_email_scheduler",
        "run_weekly_stats_email_scheduler",
        resource_class=ResourceClass.NETWORK,
    ),
    _spec(
        "webhook-retry",
        "app.workers.webhook_retry_worker",
        "run_webhook_retry_worker",
        resource_class=ResourceClass.NETWORK,
    ),
    _spec(
        "monthly-digest-scheduler",
        "app.workers.monthly_digest_scheduler",
        "run_monthly_digest_scheduler",
    ),
    _spec(
        "day-end-summary",
        "app.workers.day_end_summary_scheduler",
        "run_day_end_summary_scheduler",
    ),
    _spec("auto-backup", "app.workers.auto_backup_scheduler", "run_auto_backup_scheduler"),
    _spec(
        "audio-worker",
        "app.workers.audio_worker",
        "run_audio_worker",
        resource_class=ResourceClass.CPU,
    ),
    _spec(
        "audio-retention",
        "app.workers.audio_retention_worker",
        "run_audio_retention_worker",
    ),
    _spec(
        "hourly-card-worker",
        "app.workers.hourly_card_worker",
        "run_hourly_card_worker",
        pass_controller=False,
    ),
    _spec(
        "daily-pin-worker",
        "app.workers.daily_pin_worker",
        "run_daily_pin_worker",
        pass_controller=False,
    ),
    _spec(
        "card-enrichment-worker",
        "app.workers.card_enrichment_worker",
        "run_card_enrichment_worker",
        pass_controller=False,
    ),
    _spec(
        "tag-rule-worker",
        "app.workers.tag_rule_worker",
        "run_tag_rule_worker",
        pass_controller=False,
    ),
    _spec(
        "weekly-card-worker",
        "app.workers.weekly_card_worker",
        "run_weekly_card_worker",
        pass_controller=False,
    ),
    _spec(
        "auto-translate-worker",
        "app.workers.auto_translate_worker",
        "run_auto_translate_worker",
        pass_controller=False,
    ),
    _spec(
        "alt-text-worker",
        "app.workers.alt_text_worker",
        "run_alt_text_worker",
        pass_controller=False,
    ),
    _spec(
        "auto-pin-worker",
        "app.workers.auto_pin_worker",
        "run_auto_pin_worker",
        pass_controller=False,
    ),
    _spec(
        "entity-extractor-worker",
        "app.workers.entity_extractor_worker",
        "run_entity_extractor_worker",
        pass_controller=False,
    ),
    _spec(
        "obsidian-sync-worker",
        "app.workers.obsidian_sync_worker",
        "run_obsidian_sync_worker",
        pass_controller=False,
    ),
    _spec(
        "daily-pin-enrichment-worker",
        "app.workers.daily_pin_enrichment_worker",
        "run_daily_pin_enrichment_worker",
        pass_controller=False,
    ),
    _spec(
        "long-read-worker",
        "app.workers.long_read_worker",
        "run_long_read_worker",
        pass_controller=False,
    ),
    _spec(
        "s3-sync-worker",
        "app.workers.s3_sync_worker",
        "run_s3_sync_worker",
        pass_controller=False,
        resource_class=ResourceClass.NETWORK,
    ),
    _spec(
        "weekly-rollup-worker",
        "app.workers.weekly_rollup_worker",
        "run_weekly_rollup_worker",
        pass_controller=False,
    ),
    _spec(
        "audio-merge-worker",
        "app.workers.audio_merge_worker",
        "run_audio_merge_worker",
        pass_controller=False,
    ),
    _spec(
        "capture-session-worker",
        "app.workers.capture_session_worker",
        "run_capture_session_worker",
        pass_controller=False,
    ),
    _spec(
        "app-budget-worker",
        "app.workers.app_budget_worker",
        "run_app_budget_worker",
        pass_controller=False,
    ),
    _spec(
        "ai-reminders-worker",
        "app.workers.ai_reminders_worker",
        "run_ai_reminders_worker",
        pass_controller=False,
    ),
    _spec(
        "audit-log-rotation-worker",
        "app.workers.audit_log_rotation_worker",
        "run_audit_log_rotation_worker",
        pass_controller=False,
    ),
    _spec(
        "url-time-worker",
        "app.workers.url_time_worker",
        "run_url_time_worker",
        pass_controller=False,
    ),
    _spec(
        "smart-dedup-worker",
        "app.workers.smart_dedup_worker",
        "run_smart_dedup_worker",
        pass_controller=False,
    ),
    _spec(
        "email-weekly-digest-worker",
        "app.workers.email_weekly_digest_worker",
        "run_email_weekly_digest_worker",
        pass_controller=False,
        resource_class=ResourceClass.NETWORK,
    ),
    _spec(
        "memory-of-day-worker",
        "app.workers.memory_of_day_worker",
        "run_memory_of_day_worker",
        pass_controller=False,
    ),
    _spec(
        "dream-worker",
        "app.workers.dream_worker",
        "run_dream_worker",
        pass_controller=False,
        profiles=_FULL_AND_LEAN,
    ),
    _spec(
        "memory-projection-worker",
        "app.workers.projection_worker",
        "run_memory_projection_worker",
        pass_controller=False,
        profiles=_FULL_AND_LEAN,
        resource_class=ResourceClass.NETWORK,
        cadence="durable memory projection outbox",
    ),
    _spec(
        "telegram-worker",
        "app.workers.telegram_worker",
        "run_telegram_worker",
        pass_controller=False,
        profiles=_FULL_AND_LEAN,
        resource_class=ResourceClass.NETWORK,
        cadence="Telegram long-poll",
    ),
    _spec(
        "telegram-pinned-ingest",
        "app.integrations.telegram.pinned_ingest",
        "run_pinned_telegram_worker",
        pass_controller=False,
        profiles=_FULL_AND_LEAN,
        resource_class=ResourceClass.NETWORK,
        cadence="read-only pinned chats every 15m at night",
    ),
    _spec(
        "autowake-dispatcher",
        "app.workers.autowake_dispatcher",
        "run_owner_autowake_dispatcher",
        pass_controller=False,
        profiles=_FULL_AND_LEAN,
        resource_class=ResourceClass.NETWORK,
        cadence="durable owner outbox",
    ),
    _spec(
        "persona-impulse-producer",
        "app.workers.persona_impulse_producer",
        "run_persona_impulse_worker",
        pass_controller=False,
        profiles=_FULL_AND_LEAN,
        resource_class=ResourceClass.NETWORK,
        cadence="silent-by-default every 5m",
    ),
    _spec(
        "briefing-worker",
        "app.workers.briefing_worker",
        "run_briefing_worker",
        pass_controller=False,
    ),
    _spec(
        "heartbeat-alert-worker",
        "app.workers.heartbeat_alert_worker",
        "run_heartbeat_alert_worker",
        pass_controller=False,
    ),
    _spec(
        "db-integrity-worker",
        "app.workers.db_integrity_worker",
        "run_db_integrity_worker",
        pass_controller=False,
    ),
    _spec(
        "audio-waveform-worker",
        "app.workers.audio_waveform_worker",
        "run_audio_waveform_worker",
        pass_controller=False,
    ),
    _spec(
        "transcribe-backfill-worker",
        "app.workers.transcribe_backfill_worker",
        "run_transcribe_backfill_worker",
        pass_controller=False,
    ),
    _spec(
        "smart-pin-worker",
        "app.workers.smart_pin_worker",
        "run_smart_pin_worker",
        pass_controller=False,
    ),
    _spec(
        "tag-email-digest-worker",
        "app.workers.tag_email_digest_worker",
        "run_tag_email_digest_worker",
        pass_controller=False,
        resource_class=ResourceClass.NETWORK,
    ),
    _spec(
        "webhook-csv-worker",
        "app.workers.webhook_csv_worker",
        "run_webhook_csv_worker",
        pass_controller=False,
        resource_class=ResourceClass.NETWORK,
    ),
    _spec(
        "ocr-code-detector-worker",
        "app.workers.ocr_code_detector_worker",
        "run_ocr_code_detector_worker",
        pass_controller=False,
    ),
    _spec(
        "sync-apply-worker",
        "app.workers.sync_apply_worker",
        "run_sync_apply_worker",
        pass_controller=False,
    ),
    _spec(
        "storage-cleanup-worker",
        "app.workers.storage_cleanup_worker",
        "run",
        pass_controller=False,
    ),
)


def validate_registry(specs: Iterable[WorkerSpec]) -> None:
    """Reject ambiguous task ownership before the process starts."""

    names: set[str] = set()
    duplicates: set[str] = set()
    for spec in specs:
        if spec.name in names:
            duplicates.add(spec.name)
        names.add(spec.name)
        if not spec.profiles:
            msg = f"worker {spec.name!r} has no runtime profile"
            raise ValueError(msg)
        if spec.concurrency_limit < 1:
            msg = f"worker {spec.name!r} has an invalid concurrency limit"
            raise ValueError(msg)
    if duplicates:
        rendered = ", ".join(sorted(duplicates))
        msg = f"duplicate worker task names: {rendered}"
        raise ValueError(msg)


validate_registry(WORKER_REGISTRY)
log = get_logger("persona.bootstrap.workers")


def profile_from_environment(
    environ: Mapping[str, str],
) -> RuntimeProfile:
    """Select a fail-closed runtime profile.

    Agent-critical workers are the safe default. Starting the large legacy
    worker fleet requires an explicit ``PERSONA_RUNTIME_PROFILE=full`` (or the
    backwards-compatible explicit ``PERSONA_LEAN_MODE=0``).
    """

    configured = environ.get("PERSONA_RUNTIME_PROFILE", "").strip().lower()
    if configured:
        try:
            return RuntimeProfile(configured)
        except ValueError:
            msg = (
                "PERSONA_RUNTIME_PROFILE must be exactly 'lean' or 'full'"
            )
            raise ValueError(msg) from None
    legacy = environ.get("PERSONA_LEAN_MODE")
    if legacy is None or legacy == "1":
        return RuntimeProfile.LEAN
    if legacy == "0":
        return RuntimeProfile.FULL
    raise ValueError("PERSONA_LEAN_MODE must be exactly '0' or '1'")


def workers_for_profile(
    profile: RuntimeProfile,
    *,
    registry: Iterable[WorkerSpec] = WORKER_REGISTRY,
) -> tuple[WorkerSpec, ...]:
    """Return workers enabled for ``profile`` without importing their modules."""

    return tuple(spec for spec in registry if profile in spec.profiles)


async def run_worker_spec(spec: WorkerSpec, controller: object) -> None:
    """Resolve and execute one worker using the legacy call contract."""

    target = spec.load_target()
    if spec.pass_controller:
        await target(controller)
    else:
        await target()


class BackgroundRuntime:
    """Own worker tasks from creation through cancellation and collection."""

    def __init__(
        self,
        specs: Iterable[WorkerSpec],
        controller: object,
        *,
        runner: WorkerRunner = run_worker_spec,
        task_factory: TaskFactory = _create_task,
    ) -> None:
        self._specs = tuple(specs)
        validate_registry(self._specs)
        self._controller = controller
        self._runner = runner
        self._task_factory = task_factory
        self._tasks: list[asyncio.Task[None]] = []
        self._failure_counts: dict[str, int] = {}
        self._started = False

    @property
    def task_names(self) -> tuple[str, ...]:
        return tuple(task.get_name() for task in self._tasks)

    @property
    def tasks(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(self._tasks)

    @property
    def failure_counts(self) -> dict[str, int]:
        """Return a copy of structured worker failure counters."""
        return dict(self._failure_counts)

    def preflight(self) -> None:
        """Resolve all selected targets before the process reports readiness."""
        for spec in self._specs:
            spec.load_target()

    async def _supervise(self, spec: WorkerSpec) -> None:
        delay = 1.0
        while True:
            try:
                await self._runner(spec, self._controller)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                count = self._failure_counts.get(spec.name, 0) + 1
                self._failure_counts[spec.name] = count
                log.error(
                    "background_worker.failed",
                    worker=spec.name,
                    failure_count=count,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    restart=spec.restart_on_failure,
                    restart_delay_seconds=delay if spec.restart_on_failure else None,
                )
                if not spec.restart_on_failure:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, spec.restart_backoff_max_seconds)

    def start(self) -> tuple[asyncio.Task[None], ...]:
        """Create each selected task once while retaining partial ownership.

        A task factory can fail (for example during interpreter shutdown).  We
        append tasks as they are created so :meth:`stop` can still cancel and
        collect every task that this runtime already owns.
        """

        if self._started:
            msg = "background runtime already started"
            raise RuntimeError(msg)
        self._started = True
        self._tasks = []
        for spec in self._specs:
            coroutine = self._supervise(spec)
            try:
                task = self._task_factory(coroutine, name=spec.name)
            except BaseException:
                # The factory did not take ownership of this coroutine.
                coroutine.close()
                raise
            self._tasks.append(task)
        return self.tasks

    async def stop(self) -> None:
        """Cancel all owned tasks without letting one worker hang shutdown."""

        for task in self._tasks:
            if not task.done():
                task.cancel()

        async def collect(
            task: asyncio.Task[None],
            spec: WorkerSpec,
        ) -> None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=spec.stop_timeout_seconds,
                )
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                log.error(
                    "background_worker.stop_timeout",
                    worker=spec.name,
                    timeout_seconds=spec.stop_timeout_seconds,
                )
            except Exception as exc:
                log.warning(
                    "background_worker.stop_failed",
                    worker=spec.name,
                    error_type=type(exc).__name__,
                )

        if self._tasks:
            await asyncio.gather(
                *(
                    collect(task, spec)
                    for task, spec in zip(
                        self._tasks,
                        self._specs,
                        strict=False,
                    )
                )
            )


__all__ = [
    "WORKER_REGISTRY",
    "BackgroundRuntime",
    "ResourceClass",
    "RuntimeProfile",
    "WorkerSpec",
    "profile_from_environment",
    "run_worker_spec",
    "validate_registry",
    "workers_for_profile",
]
