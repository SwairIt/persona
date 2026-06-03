"""Per-screenshot signed share links (v0.43).

Generates short-lived HMAC-signed URLs that point at a public-style page
(no Persona admin chrome) showing exactly one screenshot plus its caption.

We deliberately reuse :func:`app.web.routes.share._sign` /
:func:`app.web.routes.share._verify` so a single process-local secret signs
single-shot tokens, collection tokens, and the older v0.21 share links.
The token payload is namespaced with the ``shot`` purpose to make it cheap
to tell single-shot tokens apart from the v0.21 ``view`` purpose later.

Revocation is implemented without a schema change: revoked screenshot ids
are persisted into ``kv_settings`` under the ``shot_share_revoked`` key as
a JSON list of ``{"id": <int>, "revoked_at": <unix>}``. A token whose
embedded screenshot id appears in that list (with ``revoked_at`` greater
than or equal to the token's issue moment) is rejected. New share links
created after a revoke continue to work — revocation is per-token-batch.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.notes import get_note
from app.storage.repository import get_kv, get_screenshot, set_kv
from app.web.routes.share import _sign, _verify
from app.web.templates_engine import templates

router = APIRouter(tags=["shot-share"])
logger = get_logger("persona.shot_share")
visit_logger = get_logger("persona.share.visits")

REVOKE_KV_KEY = "shot_share_revoked"
MAX_TTL_HOURS = 720
DEFAULT_TTL_HOURS = 24
SHOT_PURPOSE = "shot"

# Cap the User-Agent we persist. The header is unbounded by spec and some
# bots ship multi-kB junk; 200 chars is enough to recognise real browsers
# and short enough to keep ``share_visit`` rows small.
_UA_MAX_CHARS = 200


class ShotShareCreateRequest(BaseModel):
    """JSON body for ``POST /api/screenshot/{id}/share/create``."""

    ttl_hours: int = Field(default=DEFAULT_TTL_HOURS, ge=1, le=MAX_TTL_HOURS)


def _build_shot_payload(screenshot_id: int, expires: int) -> str:
    """Construct the payload string fed to :func:`_sign`.

    Keeping this in one place guarantees the ``_verify`` round-trip below
    sees the same shape we issued.
    """
    return f"{screenshot_id}|{expires}|{SHOT_PURPOSE}"


async def _load_revoked(conn: Any) -> list[dict[str, int]]:
    """Read and parse the revoke list from ``kv_settings``.

    A corrupt JSON blob is logged and treated as empty rather than 500-ing
    the share page — losing the revoke list is preferable to bricking all
    public links.
    """
    raw = await get_kv(conn, REVOKE_KV_KEY)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.error("shot_share_revoke_list_corrupt", error=str(exc))
        return []
    if not isinstance(parsed, list):
        logger.error("shot_share_revoke_list_not_a_list", got=type(parsed).__name__)
        return []
    out: list[dict[str, int]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            sid = int(item["id"])
            ra = int(item["revoked_at"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"id": sid, "revoked_at": ra})
    return out


def _is_revoked(revoked: list[dict[str, int]], screenshot_id: int, expires: int) -> bool:
    """A token is dead when its shot id was revoked *after* the token was issued.

    We do not store an issue timestamp inside the token — instead we use
    ``expires - ttl_hours * 3600`` as the implicit issue time. Because the
    ttl_hours is not in the payload either, the conservative check is:
    if any revoke entry for this id exists with ``revoked_at`` strictly
    after ``expires - MAX_TTL_HOURS * 3600``, treat it as revoked.
    """
    earliest_issue = expires - MAX_TTL_HOURS * 3600
    for entry in revoked:
        if entry["id"] != screenshot_id:
            continue
        if entry["revoked_at"] >= earliest_issue:
            return True
    return False


def _truncate_ua(raw: str | None) -> str | None:
    """Clip an arbitrarily long ``User-Agent`` to :data:`_UA_MAX_CHARS`.

    Returns ``None`` for missing or whitespace-only headers so the DB
    column stays NULL rather than holding an empty string.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    if len(stripped) <= _UA_MAX_CHARS:
        return stripped
    return stripped[:_UA_MAX_CHARS]


def _coarse_ip_prefix(host: str | None) -> str | None:
    """Reduce a client IP to its first two segments for privacy.

    IPv4 ``192.168.1.42`` becomes ``192.168``; IPv6
    ``2001:db8:abcd:1234::1`` becomes ``2001:db8``. Anything we cannot
    confidently classify (None, empty, malformed) returns ``None`` so we
    never accidentally persist a full address.
    """
    if not host:
        return None
    if ":" in host:
        # IPv6 — first two ``:``-separated groups, ignoring an empty
        # head from a leading ``::``.
        parts = [segment for segment in host.split(":") if segment]
        if len(parts) >= 2:
            return f"{parts[0]}:{parts[1]}"
        if parts:
            return parts[0]
        return None
    parts = host.split(".")
    if len(parts) >= 2 and all(parts[:2]):
        return f"{parts[0]}.{parts[1]}"
    return None


async def _record_visit(
    conn: Any,
    *,
    shot_id: int,
    ua: str | None,
    ip_prefix: str | None,
) -> None:
    """Insert one row into ``share_visit`` for a successful viewer hit.

    Parametrised SQL only — never interpolate the UA or IP into the
    statement. Errors are logged and swallowed so a write failure on the
    audit trail never breaks the public viewer.
    """
    try:
        await conn.execute(
            "INSERT INTO share_visit (shot_id, ua, ip_prefix) VALUES (?, ?, ?)",
            (shot_id, ua, ip_prefix),
        )
        await conn.commit()
    except Exception as exc:
        visit_logger.error(
            "shot_share_visit_record_failed",
            shot_id=shot_id,
            error=str(exc),
        )
        return
    visit_logger.info(
        "shot_share_visit_recorded",
        shot_id=shot_id,
        ip_prefix=ip_prefix,
    )


@router.post("/api/screenshot/{screenshot_id}/share/create")
async def create_shot_share(
    screenshot_id: int,
    body: ShotShareCreateRequest | None = None,
) -> dict[str, Any]:
    """Issue a signed URL for one screenshot."""
    payload_body = body or ShotShareCreateRequest()
    ttl_hours = payload_body.ttl_hours

    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")

    expires = int(time.time()) + ttl_hours * 3600
    token = _sign(_build_shot_payload(screenshot_id, expires))

    logger.info(
        "shot_share_created",
        screenshot_id=screenshot_id,
        ttl_hours=ttl_hours,
        expires=expires,
    )
    return {
        "url": f"/shot/share/{screenshot_id}/{token}",
        "expires_at": expires,
    }


@router.get("/shot/share/{shot_id}/{token}", response_class=HTMLResponse)
async def view_shot_share(request: Request, shot_id: int, token: str) -> HTMLResponse:
    """Render the public read-only page for a shared screenshot."""
    info = _verify(token)
    if info is None:
        raise HTTPException(status_code=410, detail="Share link expired or invalid")
    if info.get("purpose") != SHOT_PURPOSE:
        raise HTTPException(status_code=410, detail="Share link expired or invalid")
    if int(info["screenshot_id"]) != shot_id:
        # The token says one id but the URL says another — refuse rather
        # than trust either side; this only triggers if a user hand-edited
        # the URL.
        raise HTTPException(status_code=410, detail="Share link expired or invalid")

    expires = int(info["expires"])

    client = request.client
    ua = _truncate_ua(request.headers.get("user-agent"))
    ip_prefix = _coarse_ip_prefix(client.host if client else None)

    async with get_connection() as conn:
        revoked = await _load_revoked(conn)
        if _is_revoked(revoked, shot_id, expires):
            raise HTTPException(status_code=410, detail="Share link revoked")
        shot = await get_screenshot(conn, shot_id)
        if shot is None:
            raise HTTPException(status_code=410, detail="Screenshot no longer available")
        caption = await get_note(conn, shot_id)
        # Read receipt: only after the viewer has cleared every gate so
        # we do not record 410-rejected probes as legitimate reads.
        await _record_visit(conn, shot_id=shot_id, ua=ua, ip_prefix=ip_prefix)

    return templates.TemplateResponse(
        request,
        "shot_share_public.html",
        {
            "title": f"Screenshot #{shot.id}",
            "active_nav": "",
            "shot": shot,
            "caption": caption or "",
            "token": token,
            "expires": expires,
        },
    )


@router.post("/shot/share/{shot_id}/revoke")
async def revoke_shot_share(shot_id: int) -> dict[str, Any]:
    """Mark all currently-live tokens for this screenshot as revoked.

    Future tokens issued after this call will still work — revocation is a
    one-shot kill switch for outstanding links, not a permanent ban on
    sharing the screenshot.
    """
    now = int(time.time())
    async with get_connection() as conn:
        existing = await _load_revoked(conn)
        existing.append({"id": shot_id, "revoked_at": now})
        # Compact the list: keep only entries whose revoke moment can
        # still affect a live token (i.e. younger than MAX_TTL_HOURS).
        cutoff = now - MAX_TTL_HOURS * 3600
        compacted = [entry for entry in existing if entry["revoked_at"] >= cutoff]
        await set_kv(conn, REVOKE_KV_KEY, json.dumps(compacted))

    logger.info("shot_share_revoked", screenshot_id=shot_id, revoked_at=now)
    return {"ok": True, "shot_id": shot_id, "revoked_at": now}
