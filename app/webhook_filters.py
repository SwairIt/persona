"""Per-webhook event type filtering.

Each row in the ``webhooks`` table carries an ``event_types`` column —
either a comma-separated whitelist of event types or the literal ``*``
meaning "fire on every event". :func:`should_fire` is the single source
of truth for "does this webhook want this event?" and is called once
per (webhook, event) pair in the dispatcher.

The matching rules, in order:

* ``None`` / empty string / ``"*"`` → always match. This keeps existing
  rows (migration 042 defaults to ``*``) backwards-compatible: a fresh
  install fires every event at every webhook just like v0.43.
* Otherwise split the whitelist by comma, strip surrounding whitespace,
  drop empty entries. Match if the current event equals any entry
  *exactly* OR if any entry ends with ``.*`` and the current event
  starts with the entry's prefix (``screenshot.*`` matches
  ``screenshot.captured`` and ``screenshot.tagged`` but not
  ``screenshot`` on its own).

The function is deliberately synchronous and pure — no I/O, no logging
side-effects on the hot path — so the dispatcher can call it inside a
tight loop without contending for the event loop. Filtering decisions
that the operator might want to see live live in DEBUG-level structlog
records emitted by the *caller*, not here.
"""

from __future__ import annotations

from app.logging_setup import get_logger

log = get_logger("persona.webhook.filters")

_WILDCARD = "*"
_GLOB_SUFFIX = ".*"


def should_fire(webhook_event_types: str | None, current_event_type: str) -> bool:
    """Return True when a webhook with the given filter wants ``current_event_type``.

    Parameters
    ----------
    webhook_event_types:
        Raw value of the ``webhooks.event_types`` column. ``None``, the
        empty string and ``"*"`` all mean "fire on every event" so this
        is the backwards-compatible default for rows that predate the
        per-webhook filter.
    current_event_type:
        The event type currently being dispatched, e.g.
        ``"screenshot.captured"``.

    Returns
    -------
    bool
        ``True`` when the webhook should receive the event, ``False``
        when it should be silently skipped.
    """
    # Fast-path: the column is NULL / "" / "*" → fire on every event.
    if webhook_event_types is None:
        return True
    stripped = webhook_event_types.strip()
    if stripped in {"", _WILDCARD}:
        return True

    current = current_event_type.strip()
    if not current:
        # Defensive: an empty current event type can never match a
        # non-wildcard whitelist.
        return False

    matched = False
    for raw_entry in stripped.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if entry in {_WILDCARD, current}:
            # Tolerate "*" appearing inside a list (e.g. "*, screenshot.captured")
            # and short-circuit on the first exact match.
            matched = True
            break
        if entry.endswith(_GLOB_SUFFIX):
            prefix = entry[: -len(_GLOB_SUFFIX)] + "."
            if current.startswith(prefix):
                matched = True
                break
    return matched
