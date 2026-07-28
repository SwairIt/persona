"""FastAPI lifespan composition with profile-aware background workers."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol

from app.bootstrap.worker_registry import (
    BackgroundRuntime,
    RuntimeProfile,
    profile_from_environment,
    workers_for_profile,
)
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI


class CaptureController(Protocol):
    """Lifecycle operations required from the legacy capture controller."""

    def pause(self) -> None: ...

    def request_stop(self) -> None: ...


log = get_logger("persona.web")


async def _initialize_database() -> None:
    # DB/migration code is startup-only and must not inflate web import time.
    from app.storage.db import init_database  # noqa: PLC0415

    await init_database()


def _get_controller() -> CaptureController:
    # Import the narrow module, not app.workers, whose __init__ eagerly imports
    # every legacy worker implementation.
    from app.workers.control import get_controller  # noqa: PLC0415

    return get_controller()


async def _apply_pause_on_boot(controller: CaptureController) -> None:
    """Apply the existing opt-in capture pause without hiding startup failure."""

    try:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv  # noqa: PLC0415

        async with get_connection() as connection:
            pause_flag = await get_kv(connection, "capture_paused_on_boot")
        if (pause_flag or "0").strip() == "1":
            controller.pause()
            log.info("persona.boot.paused_per_setting")
    except Exception as exc:
        # This setting has historically been best-effort. Preserve that policy
        # while keeping the failure observable.
        log.warning("persona.boot.pause_check_failed", error=str(exc))


async def _shutdown_automation_resources() -> None:
    """Close browser and MCP subprocesses after worker tasks have stopped."""

    from app.browse.agent.manager import close_all  # noqa: PLC0415
    from app.mcp.runtime import shutdown_mcp_runtime  # noqa: PLC0415

    # Each optional adapter owns independent resources. One broken cleanup must
    # not prevent the other adapter from releasing its processes.
    for resource, close in (
        ("browser", close_all),
        ("mcp", shutdown_mcp_runtime),
    ):
        try:
            await close()
        except Exception as exc:
            log.warning(
                "persona.automation_shutdown_failed",
                resource=resource,
                error=str(exc),
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize resources, run the selected profile, then cleanly tear down."""

    await _initialize_database()
    controller = _get_controller()
    profile = profile_from_environment(os.environ)
    runtime = BackgroundRuntime(workers_for_profile(profile), controller)

    try:
        runtime.preflight()
        # Expose read-only lifecycle state for diagnostics without making
        # routes responsible for task ownership.
        app.state.runtime_profile = profile.value
        app.state.background_runtime = runtime

        if profile is RuntimeProfile.LEAN:
            log.warning("lifespan.lean_mode — background workers DISABLED")

        # Apply privacy state before background/capture coroutines exist. The
        # first await must not race a screenshot that the owner paused.
        await _apply_pause_on_boot(controller)
        runtime.start()

        from app.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        log.info("persona.started", host=settings.host, port=settings.port)
        yield
    finally:
        log.info("persona.stopping")
        try:
            controller.request_stop()
        except Exception as exc:
            log.warning("persona.capture_shutdown_failed", error=str(exc))
        try:
            await runtime.stop()
        finally:
            await _shutdown_automation_resources()
        log.info("persona.stopped")


__all__ = ["lifespan"]
