"""Supervised process-runtime telemetry sampler."""

from app.observability.runtime import monitor_event_loop


async def run_runtime_metrics_worker() -> None:
    await monitor_event_loop()


__all__ = ["run_runtime_metrics_worker"]
