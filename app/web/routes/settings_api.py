"""Programmatic REST surface for ``kv_settings`` (v0.86).

Three endpoints expose the same key/value store the HTML settings page
edits, so tooling (scripts, CI smoke tests, the future SDK) can read +
write preferences without screen-scraping the form:

* ``GET  /api/settings.json``         — every kv row as a flat dict.
* ``GET  /api/settings/{key}.json``   — one row, or ``404`` if absent.
* ``PUT  /api/settings/{key}.json``   — upsert ``{"value": ...}``.

Secret hygiene
--------------
Any key whose name matches the ``.*password|.*token|.*secret`` pattern
is treated as a vault-class secret and:

* never reveals its value via GET — the value is replaced with the
  literal string ``"<redacted>"``;
* refuses PUT with a ``403`` — those entries must travel through the
  encrypted vault (:mod:`app.vault`), never the plaintext kv table.

PUT is parametrised end-to-end (``set_kv`` uses ``?`` placeholders) and
every successful or rejected PUT is recorded in ``audit_log`` via
:func:`app.audit.log_action` so an operator can review who flipped a
preference during incident review. The actor field carries the client
IP (best-effort via ``request.client.host``) — there is no per-user
session model in Persona, so IP is the closest stable identity we have.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, list_kv, set_kv

router = APIRouter(tags=["settings-api"])
log = get_logger("persona.settings_api")

# Replacement value substituted for any kv whose key looks secret. The
# literal string is part of the public contract — clients use it to
# detect "this needs to come from the vault, not from /api/settings".
_REDACTED = "<redacted>"

# Case-insensitive: ``API_TOKEN``, ``user_password`` and ``slack_secret``
# all collapse to the same redaction policy. ``re.fullmatch`` against the
# key is the canonical check — sub-string matches (``"openai_token_id"``)
# are intentionally still caught because the leak risk is the same.
_REDACT_PATTERN = re.compile(r".*(?:password|token|secret).*", re.IGNORECASE)

# Bound on the ``{key}`` path parameter. kv_settings keys are short
# dotted slugs in practice (``"compact_mode"``, ``"anti_fomo_digest"``);
# anything longer than this is almost certainly a path-injection probe.
_MAX_KEY_LEN = 128

# Bound on the JSON body's ``value`` field. kv values are short strings
# (``"1"`` / ``"true"`` / a JSON-encoded list at most). 8 KiB is roomy
# for any legitimate use and stops a misbehaving client from stuffing
# multi-megabyte blobs into a plain-text settings row.
_MAX_VALUE_LEN = 8 * 1024


class _SettingPut(BaseModel):
    """Request body for ``PUT /api/settings/{key}.json``.

    ``extra="forbid"`` keeps typos like ``{"valeu": "1"}`` from silently
    succeeding with no effect — pydantic raises ``422`` instead.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    value: Annotated[str, Field(min_length=0, max_length=_MAX_VALUE_LEN)]


def _is_redacted_key(key: str) -> bool:
    """Return ``True`` if ``key`` should never expose its value via API."""
    return _REDACT_PATTERN.fullmatch(key) is not None


def _mask_dict(rows: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``rows`` with secret-shaped keys masked."""
    return {k: (_REDACTED if _is_redacted_key(k) else v) for k, v in rows.items()}


def _actor(request: Request) -> str | None:
    """Best-effort client identity for the audit log.

    Persona has no per-user session model, so the client IP is the
    closest stable identity we have. ``request.client`` can be ``None``
    when a test client builds the request without a transport, so we
    fall back to ``None`` rather than raising.
    """
    return request.client.host if request.client is not None else None


@router.get("/api/settings.json", response_class=JSONResponse)
async def settings_dump(request: Request) -> JSONResponse:
    """Return every ``kv_settings`` row as a dict, with secrets masked."""
    async with get_connection() as conn:
        rows = await list_kv(conn)
    masked = _mask_dict(rows)
    log.info(
        "settings_api.dump",
        actor=_actor(request),
        rows=len(masked),
        # Counting redacted keys (rather than naming them) keeps the
        # structured-log line useful for ops without leaking which
        # secrets are configured on this host.
        redacted=sum(1 for k in rows if _is_redacted_key(k)),
    )
    return JSONResponse(masked)


@router.get("/api/settings/{key}.json", response_class=JSONResponse)
async def settings_get_one(
    request: Request,
    key: Annotated[str, Path(min_length=1, max_length=_MAX_KEY_LEN)],
) -> JSONResponse:
    """Return one kv row, or ``404`` if the key is unset.

    Secret-shaped keys return ``<redacted>`` rather than the stored
    value. ``404`` is reserved for "the row does not exist" so a client
    can distinguish "absent" from "present but masked".
    """
    async with get_connection() as conn:
        value = await get_kv(conn, key)
    if value is None:
        log.info("settings_api.get.miss", actor=_actor(request), key=key)
        raise HTTPException(status_code=404, detail="key not found")

    redacted = _is_redacted_key(key)
    log.info(
        "settings_api.get.ok",
        actor=_actor(request),
        key=key,
        redacted=redacted,
    )
    return JSONResponse({"key": key, "value": _REDACTED if redacted else value})


@router.put("/api/settings/{key}.json", response_class=JSONResponse)
async def settings_put(
    request: Request,
    key: Annotated[str, Path(min_length=1, max_length=_MAX_KEY_LEN)],
    payload: _SettingPut,
) -> JSONResponse:
    """Upsert one kv row. Refuses secret-shaped keys.

    Returns ``200`` with the new value on success, ``403`` when the key
    matches the redaction pattern (those must use the encrypted vault),
    and ``422`` from pydantic when the body is malformed. Every PUT —
    accepted or refused — is recorded in ``audit_log``.
    """
    actor = _actor(request)

    if _is_redacted_key(key):
        await log_action(
            action="settings_api.put",
            actor=actor,
            target=key,
            detail="rejected: redacted key requires vault",
            success=False,
        )
        log.warning(
            "settings_api.put.rejected_redacted",
            actor=actor,
            key=key,
        )
        raise HTTPException(
            status_code=403,
            detail="redacted keys must be written through the vault",
        )

    async with get_connection() as conn:
        await set_kv(conn, key, payload.value)

    # ``detail`` carries only the byte length, never the plaintext —
    # the audit log is reviewed by operators and may itself be shipped
    # to a dashboard, so even "non-secret" values should not be copied
    # into it verbatim. Length + key together are enough to triage a
    # mis-set preference.
    await log_action(
        action="settings_api.put",
        actor=actor,
        target=key,
        detail=f"len={len(payload.value)}",
        success=True,
    )
    log.info(
        "settings_api.put.ok",
        actor=actor,
        key=key,
        value_len=len(payload.value),
    )
    return JSONResponse({"key": key, "value": payload.value})


__all__ = ["router"]
