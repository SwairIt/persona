"""Recycle bin UI — list soft-deleted rows, restore them, or purge early.

* ``GET  /recycle``                          renders the bin table.
* ``POST /recycle/{id}/restore``             re-inserts the row into its original table.
* ``POST /recycle/{id}/purge``               hard-deletes the row right now (no wait).
* ``POST /recycle/{id}/share-restore``       mints a 1h HMAC-signed restore link.
* ``GET  /recycle/share-restore/{id}/{tok}`` validates the link and restores.

The settings cog at the top of every page links here; the retention
worker handles the time-based purge automatically once a row crosses
``settings.recycle_retention_days``.

The share-restore flow lets an admin hand a teammate a single-shot
recovery URL without granting full bin access. Tokens are HMAC-SHA256
signed with the process-local share secret (the same ``data/.share_secret``
file as :mod:`app.web.routes.share` so we do not multiply key material),
bound to the recycle row id + an expiry stamp, and validated with
:func:`hmac.compare_digest`. TTL is fixed at one hour — the use case is
"forward this link now", not durable handoff.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.recycle import list_bin, purge_expired, restore
from app.settings import get_settings
from app.storage.db import get_connection
from app.web.routes.share import _secret
from app.web.templates_engine import templates

router = APIRouter(tags=["recycle"])
log = get_logger("persona.web.recycle")
share_log = get_logger("persona.recycle.share")
batch_log = get_logger("persona.recycle.batch")

# Bin listing cap — high enough to expose a reasonable backlog without
# letting a request page through tens of thousands of rows at once.
_LIST_LIMIT = 200

# Per-batch ceiling for restore-all. We iterate ``list_bin`` in pages
# rather than yanking every row into memory at once: this keeps a giant
# backlog (post-bulk-delete) from blowing the request's working set, and
# matches the listing cap so behaviour stays uniform with the UI.
_BATCH_PAGE = 200

# Hard ceiling on a single batch request to keep a single click from
# walking a runaway backlog. Anything beyond this needs another click —
# we deliberately do not loop until ``list_bin`` is empty.
_BATCH_MAX_ROWS = 5000

# Restore-share token tunables. The TTL is intentionally short — a
# share-restore link is meant to be forwarded once and used immediately;
# we are not building a durable revocation system.
_SHARE_TTL_SECONDS = 3600
_SHARE_PURPOSE = "recycle-restore"
# Truncate the hex digest to 32 chars (128 bits) — same shape as the
# ``app.web.routes.share`` tokens, plenty against guessing.
_SHARE_SIG_LEN = 32


def _build_share_payload(recycle_id: int, expires: int) -> str:
    """Construct the canonical payload string fed to the HMAC.

    Keeping this in one helper guarantees the signer and verifier hash
    the exact same bytes. The ``_SHARE_PURPOSE`` namespace prevents a
    token issued for some future flow from being replayed here.
    """
    return f"{recycle_id}|{expires}|{_SHARE_PURPOSE}"


def _sign_share_token(recycle_id: int, expires: int) -> str:
    """Return ``base64url(payload).hex(hmac_sha256(secret, payload))``."""
    payload = _build_share_payload(recycle_id, expires)
    digest = hmac.new(
        _secret(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:_SHARE_SIG_LEN]
    encoded = (
        base64.urlsafe_b64encode(payload.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"{encoded}.{digest}"


def _verify_share_token(  # noqa: PLR0911 — defensive bailouts are clearer than nesting
    token: str, recycle_id: int
) -> dict[str, Any] | None:
    """Validate a token issued by :func:`_sign_share_token`.

    Returns the parsed payload dict on success, ``None`` for any failure
    (bad shape, bad signature, expired, wrong purpose, wrong recycle id).
    The signature check uses :func:`hmac.compare_digest` to avoid the
    timing leak a naive ``==`` would expose.
    """
    try:
        encoded, sig = token.split(".", 1)
    except ValueError:
        return None
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(
        _secret(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:_SHARE_SIG_LEN]
    if not hmac.compare_digest(expected, sig):
        return None
    parts = payload.split("|")
    if len(parts) != 3:
        return None
    try:
        embedded_id = int(parts[0])
        expires = int(parts[1])
    except ValueError:
        return None
    purpose = parts[2]
    if purpose != _SHARE_PURPOSE:
        return None
    if embedded_id != recycle_id:
        return None
    if expires < int(time.time()):
        return None
    return {"recycle_id": embedded_id, "expires": expires, "purpose": purpose}


async def _fetch_recycle_origin(recycle_id: int) -> tuple[str, int] | None:
    """Look up ``(kind, original_id)`` for one bin row, or ``None``.

    Needed by the share-restore GET handler so we can redirect the
    recipient to the resurrected screenshot/note rather than back to the
    bin listing.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT kind, original_id FROM recycle_bin WHERE id = ?",
            (recycle_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["kind"]), int(row["original_id"])


def _redirect_target(kind: str, original_id: int) -> str:
    """Map a restored row back to its canonical detail URL.

    Falls back to ``/recycle`` for unknown kinds so a future row type
    that lands in the bin without a viewer URL still ends somewhere
    useful instead of 500-ing.
    """
    if kind == "screenshot":
        return f"/screenshot/{original_id}"
    if kind == "note":
        return f"/api/notes/{original_id}/view"
    return "/recycle"


@router.get("/recycle", response_class=HTMLResponse)
async def recycle_page(request: Request) -> HTMLResponse:
    """Render the recycle bin table."""
    settings = get_settings()
    entries = await list_bin(limit=_LIST_LIMIT)
    return templates.TemplateResponse(
        request,
        "recycle.html",
        {
            "title": "Recycle bin",
            "active_nav": "settings",
            "entries": entries,
            "retention_days": settings.recycle_retention_days,
        },
    )


@router.post("/recycle/{recycle_id}/restore")
async def recycle_restore(recycle_id: int) -> RedirectResponse:
    """Pull one row back out of the bin into its original table."""
    ok = await restore(recycle_id)
    if not ok:
        await log_action(
            "recycle.restore",
            target=str(recycle_id),
            detail="not found",
            success=False,
        )
        raise HTTPException(status_code=404, detail="Recycle bin entry not found")
    await log_action("recycle.restore", target=str(recycle_id))
    return RedirectResponse(url="/recycle", status_code=303)


@router.post("/recycle/{recycle_id}/purge")
async def recycle_purge(recycle_id: int) -> RedirectResponse:
    """Hard-delete one row from the bin right now, skipping the wait.

    Implementation reuses :func:`app.recycle.purge_expired` by first
    nudging this row's ``deleted_at`` into the far past, then asking
    purge_expired to do the actual unlink + delete. That keeps the
    "purge a file from disk" code in exactly one place.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM recycle_bin WHERE id = ?",
            (recycle_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await log_action(
                "recycle.purge",
                target=str(recycle_id),
                detail="not found",
                success=False,
            )
            raise HTTPException(status_code=404, detail="Recycle bin entry not found")
        ancient = (datetime.now(UTC) - timedelta(days=3650)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        await conn.execute(
            "UPDATE recycle_bin SET deleted_at = ? WHERE id = ?",
            (ancient, recycle_id),
        )
        await conn.commit()

    purged = await purge_expired(retention_days=1)
    await log_action(
        "recycle.purge",
        target=str(recycle_id),
        detail=f"purged_in_batch={purged}",
    )
    return RedirectResponse(url="/recycle", status_code=303)


@router.post("/recycle/restore-all")
async def recycle_restore_all(confirm: int = 0) -> RedirectResponse:
    """Pull every row in the bin back into its original table.

    Requires ``?confirm=1`` so a stray fetch (browser prefetch, link
    follower) cannot empty the bin by accident — the template wires the
    flag through a vanilla JS ``confirm()`` dialog.

    Walks the bin in pages of ``_BATCH_PAGE`` rather than loading every
    row at once, and stops at ``_BATCH_MAX_ROWS`` so a single click
    cannot consume an unbounded amount of work. Individual restore
    failures are counted but do not abort the batch — one corrupt row
    should not strand the rest in the bin.
    """
    if confirm != 1:
        batch_log.warning("restore_all.unconfirmed")
        await log_action(
            "recycle.restore_all",
            detail="missing confirm",
            success=False,
        )
        raise HTTPException(status_code=400, detail="confirm=1 query param required")

    restored = 0
    failed = 0
    while restored + failed < _BATCH_MAX_ROWS:
        entries = await list_bin(limit=_BATCH_PAGE)
        if not entries:
            break
        progressed = False
        for entry in entries:
            if restored + failed >= _BATCH_MAX_ROWS:
                break
            try:
                ok = await restore(entry["id"])
            except Exception:
                # Keep the batch alive on per-row failure — one corrupt
                # row should not strand the rest of the bin.
                batch_log.exception("restore_all.row_failed", recycle_id=entry["id"])
                failed += 1
                progressed = True
                continue
            if ok:
                restored += 1
                progressed = True
            else:
                # Row vanished between the listing and the restore call
                # (concurrent purge / share-restore). Skip it and move on.
                failed += 1
                progressed = True
        if not progressed:
            # Defensive: every row in the page refused to advance, which
            # should be impossible given the branches above. Bail out
            # rather than spin forever.
            break

    batch_log.info("restore_all", restored=restored, failed=failed)
    await log_action(
        "recycle.restore_all",
        target="*",
        detail=f"restored={restored} failed={failed}",
        success=failed == 0,
    )
    return RedirectResponse(url="/recycle", status_code=303)


@router.post("/recycle/purge-all")
async def recycle_purge_all(confirm: int = 0) -> RedirectResponse:
    """Hard-delete every row currently in the bin.

    Requires ``?confirm=1`` for the same reason as ``restore-all`` — a
    stray request must not nuke the bin. Implementation reuses
    :func:`app.recycle.purge_expired` exactly like the per-row purge
    endpoint: backdate every row's ``deleted_at`` so the existing
    retention path treats the entire bin as expired, then call
    ``purge_expired(retention_days=1)`` once. That keeps the on-disk
    thumbnail unlink + storage-savings credit in a single place.
    """
    if confirm != 1:
        batch_log.warning("purge_all.unconfirmed")
        await log_action(
            "recycle.purge_all",
            detail="missing confirm",
            success=False,
        )
        raise HTTPException(status_code=400, detail="confirm=1 query param required")

    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM recycle_bin")
        count_row = await cursor.fetchone()
        total = int(count_row["n"]) if count_row is not None else 0
        if total == 0:
            batch_log.info("purge_all.empty")
            await log_action(
                "recycle.purge_all",
                target="*",
                detail="empty bin",
            )
            return RedirectResponse(url="/recycle", status_code=303)
        ancient = (datetime.now(UTC) - timedelta(days=3650)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        await conn.execute(
            "UPDATE recycle_bin SET deleted_at = ?",
            (ancient,),
        )
        await conn.commit()

    bytes_freed = await purge_expired(retention_days=1)
    batch_log.info("purge_all", rows=total, bytes_freed=bytes_freed)
    await log_action(
        "recycle.purge_all",
        target="*",
        detail=f"rows={total} bytes_freed={bytes_freed}",
    )
    return RedirectResponse(url="/recycle", status_code=303)


@router.post("/recycle/{recycle_id}/share-restore")
async def recycle_share_restore_create(
    recycle_id: int, request: Request
) -> dict[str, Any]:
    """Mint a 1h HMAC-signed restore URL for one bin row.

    The returned ``url`` is a relative path — the caller's own browser
    typically renders it as an absolute URL when copied. We deliberately
    do not bake the host into the signed payload so a Persona instance
    reachable under multiple hostnames (LAN IP + tunnel) keeps working.
    """
    origin = await _fetch_recycle_origin(recycle_id)
    if origin is None:
        share_log.warning("share_restore.create.missing", recycle_id=recycle_id)
        await log_action(
            "recycle.share_restore.create",
            target=str(recycle_id),
            detail="not found",
            success=False,
        )
        raise HTTPException(status_code=404, detail="Recycle bin entry not found")

    expires = int(time.time()) + _SHARE_TTL_SECONDS
    token = _sign_share_token(recycle_id, expires)
    url = f"/recycle/share-restore/{recycle_id}/{token}"

    kind, original_id = origin
    share_log.info(
        "share_restore.create",
        recycle_id=recycle_id,
        kind=kind,
        original_id=original_id,
        expires=expires,
        ttl_seconds=_SHARE_TTL_SECONDS,
    )
    await log_action(
        "recycle.share_restore.create",
        target=str(recycle_id),
        detail=f"kind={kind} expires={expires}",
    )
    return {
        "url": url,
        "expires_at": expires,
        "ttl_seconds": _SHARE_TTL_SECONDS,
    }


@router.get("/recycle/share-restore/{recycle_id}/{token}")
async def recycle_share_restore_consume(
    recycle_id: int, token: str
) -> RedirectResponse:
    """Validate ``token`` then restore ``recycle_id`` and redirect.

    Failure modes are deliberately collapsed into a single 410 message
    so a teammate who got a stale link cannot tell whether the token was
    forged, expired, already used, or simply for a different row — all
    cases produce the same response.
    """
    # Capture the origin *before* we restore — once :func:`restore`
    # commits, the recycle row is gone and ``_fetch_recycle_origin``
    # cannot tell us where to send the recipient.
    origin = await _fetch_recycle_origin(recycle_id)
    info = _verify_share_token(token, recycle_id)
    if info is None or origin is None:
        share_log.warning(
            "share_restore.consume.rejected",
            recycle_id=recycle_id,
            token_known=info is not None,
            row_present=origin is not None,
        )
        await log_action(
            "recycle.share_restore.consume",
            target=str(recycle_id),
            detail="invalid or expired",
            success=False,
        )
        raise HTTPException(status_code=410, detail="Restore link expired or invalid")

    kind, original_id = origin
    ok = await restore(recycle_id)
    if not ok:
        # Race: another caller restored or purged this row between the
        # origin lookup and the restore call. Treat as gone.
        share_log.warning("share_restore.consume.race", recycle_id=recycle_id)
        await log_action(
            "recycle.share_restore.consume",
            target=str(recycle_id),
            detail="restore failed",
            success=False,
        )
        raise HTTPException(status_code=410, detail="Restore link expired or invalid")

    share_log.info(
        "share_restore.consume",
        recycle_id=recycle_id,
        kind=kind,
        original_id=original_id,
    )
    await log_action(
        "recycle.share_restore.consume",
        target=str(recycle_id),
        detail=f"kind={kind} original_id={original_id}",
    )
    return RedirectResponse(
        url=_redirect_target(kind, original_id), status_code=303
    )
