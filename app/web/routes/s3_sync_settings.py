"""HTTP surface for the S3-compatible cross-machine sync feature.

Three endpoints, mirroring the Obsidian sync settings shape:

* ``GET  /settings/s3-sync`` — render the configuration form.
* ``POST /settings/s3-sync`` — persist the form into ``kv_settings``.
  The secret-key field is HTML-masked AND we deliberately scrub it from
  every structured log line emitted by this module (see ``_safe_log``
  helper below). A leaked secret in our logs would be just as bad as a
  leak on disk.
* ``POST /api/s3-sync/run-now`` — fire :func:`sync_to_s3` once and
  return the result. The button on the settings page calls this and
  paints the JSON inline.

State lives in seven kv_settings rows (matches ``app.s3_sync._kv_keys``
+ ``app.workers.s3_sync_worker``):

============================  =========================  =================
kv key                         form field                 shape
============================  =========================  =================
``s3_sync_enabled``            checkbox ``enabled``       ``"0"`` / ``"1"``
``s3_sync_hour_local``         number ``hour``            int 0..23
``s3_sync_bucket``             text ``bucket``            string
``s3_sync_prefix``             text ``prefix``            string (no slash)
``s3_sync_access_key``         text ``access_key``        string
``s3_sync_secret_key``         password ``secret_key``    string (masked)
``s3_sync_endpoint_url``       text ``endpoint_url``      URL (optional)
``s3_sync_passphrase``         password ``passphrase``    string (masked)
============================  =========================  =================
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.s3_sync import sync_to_s3
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

log = get_logger("persona.s3_sync.web")

router = APIRouter(tags=["s3-sync-settings"])

# ---------------------------------------------------------------------------
# kv keys — duplicated as Final[str] constants to keep the module
# self-contained (importing private names from app.s3_sync would couple
# the routing layer to the sync internals more than necessary).
# ---------------------------------------------------------------------------

_KV_ENABLED: Final[str] = "s3_sync_enabled"
_KV_HOUR_LOCAL: Final[str] = "s3_sync_hour_local"
_KV_BUCKET: Final[str] = "s3_sync_bucket"
_KV_PREFIX: Final[str] = "s3_sync_prefix"
_KV_ACCESS_KEY: Final[str] = "s3_sync_access_key"
_KV_SECRET_KEY: Final[str] = "s3_sync_secret_key"  # noqa: S105 — kv row NAME, not a secret value
_KV_ENDPOINT: Final[str] = "s3_sync_endpoint_url"
_KV_PASSPHRASE: Final[str] = "s3_sync_passphrase"  # noqa: S105 — kv row NAME, not a secret value

_HOUR_MIN: Final[int] = 0
_HOUR_MAX: Final[int] = 23
_HOUR_DEFAULT: Final[int] = 3


# ---------------------------------------------------------------------------
# Form coercion helpers
# ---------------------------------------------------------------------------


def _parse_checkbox(value: str) -> bool:
    cleaned = value.strip().lower()
    return cleaned in {"1", "on", "true", "yes"}


def _read_bool(raw: str | None) -> bool:
    return (raw or "").strip() == "1"


def _read_hour(raw: str | None) -> int:
    """Parse hour 0..23 from a kv string. Out-of-range falls back to default."""
    if raw is None:
        return _HOUR_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        return _HOUR_DEFAULT
    if value < _HOUR_MIN:
        return _HOUR_MIN
    if value > _HOUR_MAX:
        return _HOUR_MAX
    return value


def _safe_log_payload(**fields: Any) -> dict[str, Any]:
    """Drop any field whose name contains 'secret' or 'passphrase'.

    Belt-and-braces: every call site in this module already avoids
    passing the secret fields, but the central scrubber gives one
    obvious place to enforce the contract.
    """
    return {
        key: value
        for key, value in fields.items()
        if "secret" not in key.lower() and "passphrase" not in key.lower()
    }


# ---------------------------------------------------------------------------
# GET /settings/s3-sync
# ---------------------------------------------------------------------------


@router.get("/settings/s3-sync", response_class=HTMLResponse)
async def s3_sync_settings_page(request: Request) -> HTMLResponse:
    """Render the form. Secret-key + passphrase fields are blanked.

    We never round-trip the secret values back into the form. Instead
    we show a placeholder ("●●●●●● — leave blank to keep current") so
    the user knows a value is configured without us re-rendering it
    in plaintext (e.g. into a downloaded HTML save).
    """
    async with get_connection() as conn:
        enabled_raw = await get_kv(conn, _KV_ENABLED)
        hour_raw = await get_kv(conn, _KV_HOUR_LOCAL)
        bucket_raw = await get_kv(conn, _KV_BUCKET)
        prefix_raw = await get_kv(conn, _KV_PREFIX)
        access_key_raw = await get_kv(conn, _KV_ACCESS_KEY)
        secret_key_raw = await get_kv(conn, _KV_SECRET_KEY)
        endpoint_raw = await get_kv(conn, _KV_ENDPOINT)
        passphrase_raw = await get_kv(conn, _KV_PASSPHRASE)

    enabled = _read_bool(enabled_raw)
    hour = _read_hour(hour_raw)
    bucket = (bucket_raw or "").strip()
    prefix = (prefix_raw or "").strip()
    access_key = (access_key_raw or "").strip()
    endpoint = (endpoint_raw or "").strip()
    has_secret_key = bool((secret_key_raw or "").strip())
    has_passphrase = bool((passphrase_raw or "").strip())

    log.info(
        "s3_sync.settings.page",
        **_safe_log_payload(
            enabled=enabled,
            hour=hour,
            has_bucket=bool(bucket),
            has_prefix=bool(prefix),
            has_access_key=bool(access_key),
            has_endpoint=bool(endpoint),
            has_secret_key=has_secret_key,
            has_passphrase=has_passphrase,
        ),
    )

    return templates.TemplateResponse(
        request,
        "s3_sync_settings.html",
        {
            "title": "S3-синхронизация",
            "active_nav": "settings",
            "enabled": enabled,
            "hour": hour,
            "hour_min": _HOUR_MIN,
            "hour_max": _HOUR_MAX,
            "bucket": bucket,
            "prefix": prefix,
            "access_key": access_key,
            "endpoint_url": endpoint,
            "has_secret_key": has_secret_key,
            "has_passphrase": has_passphrase,
        },
    )


# ---------------------------------------------------------------------------
# POST /settings/s3-sync
# ---------------------------------------------------------------------------


@router.post("/settings/s3-sync", response_class=HTMLResponse)
async def s3_sync_settings_save(
    request: Request,
    bucket: str = Form(default=""),
    prefix: str = Form(default=""),
    access_key: str = Form(default=""),
    secret_key: str = Form(default=""),
    endpoint_url: str = Form(default=""),
    passphrase: str = Form(default=""),
    hour: str = Form(default=""),
    enabled: str = Form(default=""),
) -> RedirectResponse:
    """Persist all eight kv rows. Never logs ``secret_key`` or ``passphrase``.

    Empty ``secret_key`` / ``passphrase`` mean *keep the existing value*
    — we deliberately don't overwrite a stored secret with the blank
    placeholder the GET handler ships down. That way the user can edit
    other fields (e.g. bump the hour) without re-typing credentials.
    """
    bucket_value = bucket.strip()
    prefix_value = prefix.strip().lstrip("/").rstrip("/")
    access_key_value = access_key.strip()
    endpoint_value = endpoint_url.strip()
    hour_value = _read_hour(hour)
    enabled_value = _parse_checkbox(enabled)

    async with get_connection() as conn:
        await set_kv(conn, _KV_ENABLED, "1" if enabled_value else "0")
        await set_kv(conn, _KV_HOUR_LOCAL, str(hour_value))
        await set_kv(conn, _KV_BUCKET, bucket_value)
        await set_kv(conn, _KV_PREFIX, prefix_value)
        await set_kv(conn, _KV_ACCESS_KEY, access_key_value)
        await set_kv(conn, _KV_ENDPOINT, endpoint_value)
        # Only overwrite secrets when the user actually typed something.
        if secret_key.strip():
            await set_kv(conn, _KV_SECRET_KEY, secret_key.strip())
        if passphrase.strip():
            await set_kv(conn, _KV_PASSPHRASE, passphrase.strip())

    log.info(
        "s3_sync.settings.save",
        **_safe_log_payload(
            enabled=enabled_value,
            hour=hour_value,
            bucket=bucket_value,
            prefix=prefix_value,
            has_access_key=bool(access_key_value),
            endpoint_url=endpoint_value,
            secret_key_updated=bool(secret_key.strip()),
            passphrase_updated=bool(passphrase.strip()),
        ),
    )

    return RedirectResponse(url="/settings/s3-sync", status_code=303)


# ---------------------------------------------------------------------------
# POST /api/s3-sync/run-now
# ---------------------------------------------------------------------------


@router.post("/api/s3-sync/run-now", response_class=JSONResponse)
async def s3_sync_run_now(request: Request) -> JSONResponse:
    """Trigger a one-off sync and return the result dict.

    Returns 200 even when the sync degraded gracefully (missing deps /
    missing config) — the JSON ``status`` field tells the caller what
    happened. A non-200 only fires when ``sync_to_s3`` itself raises,
    which it should never do (it traps all exceptions internally and
    returns ``status="error"``).
    """
    result = await sync_to_s3()
    status = str(result.get("status", "unknown"))

    log.info(
        "s3_sync.run_now",
        status=status,
        db_uploaded=result.get("db_uploaded", 0),
        thumbnails_uploaded=result.get("thumbnails_uploaded", 0),
        bytes_total=result.get("bytes_total", 0),
        error=result.get("error"),
    )

    body: dict[str, Any] = {
        "ok": status == "ok",
        "status": status,
        "db_uploaded": int(result.get("db_uploaded", 0)),
        "thumbnails_uploaded": int(result.get("thumbnails_uploaded", 0)),
        "bytes_total": int(result.get("bytes_total", 0)),
    }
    if "error" in result:
        body["error"] = result["error"]
    return JSONResponse(body)


__all__ = ["router"]
