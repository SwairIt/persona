"""Power-state detection via ``psutil`` with graceful fallbacks.

Returns a small dict describing AC/battery state. Used by the capture loop
to slow down or pause when the host is running on battery.
"""

from __future__ import annotations

from typing import TypedDict

import anyio.to_thread
import psutil


class PowerState(TypedDict):
    """Snapshot of the host's power status."""

    on_battery: bool
    percent: float | None
    plugged: bool | None


_FALLBACK: PowerState = {"on_battery": False, "percent": None, "plugged": None}


def get_power_state() -> PowerState:
    """Return the current power state.

    Falls back to ``{"on_battery": False, "percent": None, "plugged": None}``
    when no battery is present (desktop hardware, Linux without
    ``/sys/class/power_supply``) or when ``psutil`` raises any exception.
    """
    try:
        battery = psutil.sensors_battery()
    except Exception:
        # psutil may raise platform-specific errors on exotic hardware
        return dict(_FALLBACK)  # type: ignore[return-value]

    if battery is None:
        return dict(_FALLBACK)  # type: ignore[return-value]

    plugged = bool(battery.power_plugged) if battery.power_plugged is not None else None
    on_battery = plugged is False
    percent = float(battery.percent) if battery.percent is not None else None

    return {
        "on_battery": on_battery,
        "percent": percent,
        "plugged": plugged,
    }


async def get_power_state_async() -> PowerState:
    """Async wrapper around :func:`get_power_state` via ``anyio.to_thread``."""
    return await anyio.to_thread.run_sync(get_power_state)
