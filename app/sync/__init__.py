"""Multi-device sync — event log + pull/push reconciliation.

This package owns the server-side substrate for local-first sync. The
short version:

  * Every mutation on a syncable entity (notes, tags, annotations, kv,
    pins) becomes an append-only ``sync_event`` row.
  * Devices POST their local mutations to ``/api/sync/push`` and pull
    other devices' events via ``/api/sync/pull?since=N``.
  * Conflict resolution is last-write-wins on ``logical_clock`` (Lamport
    timestamp). Tied clocks are broken by ``device_id`` ascending so the
    decision is deterministic.

The package does NOT yet wire mutations into the existing route handlers
(notes / tags / annotations / kv). That comes next — for this tick we
ship the substrate, the API, and an admin page so the user can see what's
flowing.
"""

from app.sync.events import (
    append_event,
    list_events_since,
)
from app.sync.reconcile import apply_pending
from app.sync.state import (
    bump_pulled_watermark,
    bump_pushed_clock,
    get_state,
)

__all__ = [
    "append_event",
    "apply_pending",
    "bump_pulled_watermark",
    "bump_pushed_clock",
    "get_state",
    "list_events_since",
]
