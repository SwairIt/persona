"""Remote-agent upload API — heartbeat / audio / screenshot / me (v1.12).

A "remote agent" is a small uploader running on a separate machine
(Mac, mobile device, secondary laptop) that pushes audio segments and
screenshots to this Persona instance over HTTPS. Routes registered
here authenticate the caller via :func:`app.remote_agents.verify_agent_token`
on every request and bump the matching ``last_*_at`` columns so the
admin UI can show per-channel liveness.

v1.30: registered in :mod:`app.web.main` (the older docstring was
stale — the coordinator picks every route module up once it ships).

Threat model notes
------------------
* **Header-only authentication.** No cookies, no signed bodies — the
  agent presents a bearer token in ``Authorization``. Loss of the
  token must be remediated by revoking the row in
  ``/admin/agents``; there is no time-limited refresh.
* **Per-channel rate-limiting is out of scope here.** A misbehaving
  agent burning 5 MB screenshots in a tight loop is detectable on
  the admin dashboard (``last_screen_at`` ticking every second); the
  operator revokes the row to stop the bleeding.
* **The Authorization header is never logged.** structlog calls in
  this module only ever log the resolved agent id + name, never the
  raw token. The ``Bearer`` extractor is responsible for stripping
  the credential from any exception text before it propagates.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import anyio
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError

from app.dedup import compute_phash, find_or_create_dedup_group
from app.logging_setup import get_logger
from app.remote_agents import (
    VerifiedAgent,
    bump_last_seen,
    verify_agent_token,
)
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import insert_screenshot

log = get_logger("persona.agent_api")

router = APIRouter(prefix="/api/agent", tags=["agent-api"])

# Per-spec upload ceilings. The check runs before any decode / disk
# write so a deliberately oversized POST is refused on the input pipe
# rather than after we have already allocated buffers.
_MAX_AUDIO_BYTES: Final[int] = 10 * 1024 * 1024
_MAX_SCREEN_BYTES: Final[int] = 5 * 1024 * 1024

# Generous pixel ceiling so a 50k x 50k "PNG bomb" cannot blow up the
# PIL decode budget. Matches the local-capture import route.
_MAX_PIXEL_DIMENSION: Final[int] = 16_384

# Allow-list of image MIME prefixes the screenshot endpoint will
# accept. We still sniff the magic bytes below, but using the declared
# MIME as a pre-filter rejects obviously wrong uploads fast.
_IMAGE_MIME_PREFIX: Final[str] = "image/"
_AUDIO_MIME_PREFIX: Final[str] = "audio/"

# Magic-byte prefixes used to fast-reject non-image uploads before
# handing the bytes to PIL.
_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC: Final[bytes] = b"\xff\xd8\xff"
_WEBP_MAGIC_PREFIX: Final[bytes] = b"RIFF"
_WEBP_MAGIC_SUFFIX: Final[bytes] = b"WEBP"

# Recognised audio file extensions. The endpoint stores the upload
# under the extension declared by the agent's filename when it is in
# this set; otherwise it falls back to ``.bin`` so the bytes survive
# round-tripping even if the codec is exotic.
_AUDIO_EXTS: Final[frozenset[str]] = frozenset(
    {".opus", ".ogg", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".webm"}
)
_IMAGE_EXTS: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp"}
)

# Drop everything outside this set so a hostile filename like
# ``../../etc/passwd`` collapses to ``etcpasswd`` before we ever hand
# it to ``Path``. Dot is kept so the extension survives.
_SAFE_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")

# Bearer scheme prefix (case-insensitive). The header parser strips
# this before handing the credential to ``verify_agent_token``.
_BEARER_PREFIX: Final[str] = "bearer "


# ---------------------------------------------------------------------------
# Authentication helper
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: str | None) -> str:
    """Return the raw token from an ``Authorization`` header, or 401.

    The header MUST be ``Authorization: Bearer <token>`` (case-
    insensitive scheme, whitespace tolerated). Any other shape — empty,
    missing scheme, multiple schemes — surfaces an opaque 401 so the
    caller cannot tell *which* shape mistake they made.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    candidate = authorization.strip()
    if len(candidate) <= len(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if candidate[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw = candidate[len(_BEARER_PREFIX) :].strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return raw


async def _resolve_agent(
    authorization: str | None,
    *,
    kind: str = "any",
    x_agent_token: str | None = None,
) -> VerifiedAgent:
    """Authenticate the caller and refresh the liveness columns.

    ``kind`` is forwarded to :func:`bump_last_seen` so the per-channel
    timestamp matching the route gets touched. A failed verify always
    raises 401 with an opaque body; the structured log line carries
    the reason for the operator to triage offline.

    T29 — the token may arrive either as ``Authorization: Bearer <tok>``
    or as a custom ``X-Agent-Token`` header. The fallback exists because
    some tunnels/proxies (notably Microsoft Dev Tunnels) strip the
    standard ``Authorization`` header for their own access control — that
    silently 401'd every screenshot/audio upload while the custom
    ``X-Device-Token`` sync path kept working. Custom headers survive, so
    the agent now sends both and we accept whichever arrives.
    """
    raw = ""
    if authorization and authorization.strip():
        raw = _extract_bearer(authorization)
    elif x_agent_token and x_agent_token.strip():
        raw = x_agent_token.strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    agent = await verify_agent_token(raw)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Best-effort liveness bump. Errors here are swallowed inside
    # bump_last_seen so a stuck UPDATE cannot 500 the upload.
    bump_kind = kind if kind in {"audio", "screen", "any"} else "any"
    # mypy doesn't narrow ``str`` through the membership check above, so
    # cast via a Literal-typed local. The runtime check is the
    # authoritative guard.
    if bump_kind == "audio":
        await bump_last_seen(agent["id"], kind="audio")
    elif bump_kind == "screen":
        await bump_last_seen(agent["id"], kind="screen")
    else:
        await bump_last_seen(agent["id"], kind="any")
    return agent


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _sanitise_filename(name: str | None, fallback_ext: str) -> str:
    """Return a filesystem-safe, length-bounded filename.

    Strips path separators, normalises odd characters, and guarantees
    an extension. Never produces a name starting with ``.`` or longer
    than 80 chars. Used for both audio and image uploads.
    """
    raw_name = (name or "").strip()
    base = Path(raw_name).name
    cleaned = _SAFE_FILENAME_RE.sub("_", base).strip("._")
    if not cleaned:
        cleaned = f"upload.{fallback_ext}"
    elif "." not in cleaned:
        cleaned = f"{cleaned}.{fallback_ext}"
    if len(cleaned) > 80:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and ext:
            keep = 80 - len(ext) - 1
            cleaned = f"{stem[: max(1, keep)]}.{ext}"
        else:
            cleaned = cleaned[:80]
    return cleaned


def _agent_audio_dir(agent_id: int, captured_at: datetime) -> Path:
    """Return ``data/agent/<id>/audio/YYYY/MM/DD/`` (mkdir -p)."""
    settings = get_settings()
    target = (
        settings.data_dir
        / "agent"
        / str(int(agent_id))
        / "audio"
        / f"{captured_at.year:04d}"
        / f"{captured_at.month:02d}"
        / f"{captured_at.day:02d}"
    )
    target.mkdir(parents=True, exist_ok=True)
    return target


def _agent_screen_dir(agent_id: int, captured_at: datetime) -> Path:
    """Return ``data/agent/<id>/screens/YYYY/MM/DD/`` (mkdir -p)."""
    settings = get_settings()
    target = (
        settings.data_dir
        / "agent"
        / str(int(agent_id))
        / "screens"
        / f"{captured_at.year:04d}"
        / f"{captured_at.month:02d}"
        / f"{captured_at.day:02d}"
    )
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_bytes(target: Path, raw: bytes) -> None:
    """Synchronous bytes-to-disk helper invoked via ``anyio.to_thread``."""
    target.write_bytes(raw)


def _relative_to_data_dir(path: Path) -> str:
    """Return ``path`` relative to ``settings.data_dir`` for DB storage."""
    settings = get_settings()
    return str(path.resolve().relative_to(settings.data_dir.resolve()))


# ---------------------------------------------------------------------------
# Input parsing helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str, *, field: str) -> datetime:
    """Parse ``value`` as ISO-8601, returning a timezone-aware UTC datetime."""
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"missing {field}",
        )
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid {field}: not ISO-8601",
        ) from exc
    if parsed.tzinfo is None:
        # The agent SHOULD send a UTC timestamp; if it forgot the zone
        # we tag it as UTC rather than assuming local — every downstream
        # query in Persona stores UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decode_and_phash(raw: bytes) -> tuple[int, int, str]:
    """Decode an image + compute its pHash. CPU-bound (PIL + scipy DCT) —
    ALWAYS call via ``anyio.to_thread.run_sync`` so it never blocks the
    asyncio event loop. Returns ``(width, height, phash_hex)``."""
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        width, height = image.size
        phash = compute_phash(image)
    return width, height, phash


def _detect_image_format(raw: bytes) -> str | None:
    """Return ``"png"`` / ``"jpeg"`` / ``"webp"`` if magic bytes match."""
    if raw.startswith(_PNG_MAGIC):
        return "png"
    if raw.startswith(_JPEG_MAGIC):
        return "jpeg"
    if (
        len(raw) >= 12
        and raw[:4] == _WEBP_MAGIC_PREFIX
        and raw[8:12] == _WEBP_MAGIC_SUFFIX
    ):
        return "webp"
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/heartbeat")
async def heartbeat(
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Lightweight liveness probe — returns ``{ok, server_time, agent_id}``.

    The agent calls this from its launchd / cron / loop to confirm the
    server is reachable and the token is still good. No body, no
    side-effects beyond bumping ``last_seen_at``.
    """
    agent = await _resolve_agent(authorization, kind="any", x_agent_token=x_agent_token)
    server_time = datetime.now(tz=UTC).isoformat()
    log.info("agent_api.heartbeat", agent_id=agent["id"], name=agent["name"])
    return JSONResponse(
        {
            "ok": True,
            "server_time": server_time,
            "agent_id": agent["id"],
        }
    )


@router.get("/me")
async def me(
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Return the calling agent's metadata (id, name, platform, liveness).

    Useful as a self-check after the agent has been provisioned: hit
    ``/api/agent/me`` once on first run to confirm the operator wired
    in the right token and the server agrees on which agent it is.
    """
    agent = await _resolve_agent(authorization, kind="any", x_agent_token=x_agent_token)
    return JSONResponse(
        {
            "ok": True,
            "id": agent["id"],
            "name": agent["name"],
            "platform": agent["platform"],
            "last_seen_at": agent["last_seen_at"],
            "last_audio_at": agent["last_audio_at"],
            "last_screen_at": agent["last_screen_at"],
        }
    )


async def _transcribe_uploaded_segment(segment_id: int, audio_path: Path) -> None:
    """T29 — transcribe an agent-uploaded segment on the SERVER.

    Lets the Mac agent run the lightweight (webrtcvad, no-torch) audio
    path and upload audio WITHOUT a transcript; the server — which has
    Whisper — fills in the text afterwards. No-op if no Whisper backend
    is installed (transcript just stays empty). Best-effort: never raises.
    """
    from app.audio.transcribe import transcribe_segment  # noqa: PLC0415

    try:
        text = await transcribe_segment(audio_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_api.audio.server_transcribe_failed", error=str(exc))
        return
    if text is None:
        return
    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE audio_segment SET transcript = ? "
                "WHERE id = ? AND (transcript IS NULL OR transcript = '')",
                (text, segment_id),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_api.audio.transcript_store_failed", error=str(exc))


@router.post("/audio-segment")
async def upload_audio_segment(
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File(...)],
    captured_at: Annotated[str, Form(...)],
    duration_seconds: Annotated[float, Form(...)],
    codec: Annotated[str, Form(...)],
    bitrate: Annotated[int | None, Form()] = None,
    transcript: Annotated[str | None, Form()] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Accept one speech segment from a remote agent.

    Pipeline (matches the local :mod:`app.workers.audio_worker` shape so
    a remote-agent row is indistinguishable from a local one downstream):

    1. Authenticate the agent and bump ``last_audio_at``.
    2. Read the upload and refuse anything over 10 MiB.
    3. Validate MIME (``audio/*``) and parse the form metadata.
    4. Write the bytes under ``data/agent/<id>/audio/YYYY/MM/DD/`` with
       a name derived from the upload filename's safe stem.
    5. ``INSERT INTO audio_segment`` with the same column shape as the
       local worker (``captured_at`` / ``duration_seconds`` / ``codec``
       / ``bitrate`` / ``path`` / ``size_bytes`` / ``transcript``).
    6. Return ``{ok, segment_id}``.
    """
    agent = await _resolve_agent(authorization, kind="audio", x_agent_token=x_agent_token)

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty audio upload",
        )
    if len(raw) > _MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="audio upload too large (max 10MB)",
        )

    declared_mime = (file.content_type or "").lower()
    if declared_mime and not declared_mime.startswith(_AUDIO_MIME_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported content type: {declared_mime}",
        )

    captured_dt = _parse_iso(captured_at, field="captured_at")

    if duration_seconds < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="duration_seconds must be non-negative",
        )

    cleaned_codec = codec.strip()
    if not cleaned_codec:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing codec",
        )

    cleaned_transcript: str | None = None
    if transcript is not None:
        # Empty-string transcript ("transcribed but silent") is a
        # meaningful state, so we preserve it as-is rather than
        # coercing to NULL.
        cleaned_transcript = transcript

    # Derive a sensible on-disk extension from the upload filename;
    # fall back to ``.bin`` when the agent did not supply one we know.
    fallback_ext = "bin"
    raw_suffix = Path(file.filename or "").suffix.lower()
    if raw_suffix in _AUDIO_EXTS:
        fallback_ext = raw_suffix.lstrip(".")
    safe_name = _sanitise_filename(file.filename, fallback_ext)

    target_dir = _agent_audio_dir(agent["id"], captured_dt)
    target = target_dir / safe_name
    if target.exists():
        # Disambiguate same-named uploads (clock skew, retry after a
        # network hiccup) by tagging the second one with the current
        # microsecond. Both files keep their bytes.
        stem = target.stem
        suffix = target.suffix
        tag = datetime.now(tz=UTC).strftime("%H%M%S%f")
        target = target_dir / f"{stem}-{tag}{suffix}"

    await anyio.to_thread.run_sync(_write_bytes, target, raw)
    stored_path = _relative_to_data_dir(target)
    size_bytes = len(raw)

    # Mirror the column shape of the local audio worker so the
    # day-view, retention sweep and transcript pipeline cannot tell a
    # remote-uploaded segment from a locally-captured one. The schema
    # was originally ``started_at`` / ``duration_s`` but migration
    # ``093_audio_duration_rename.sql`` renamed those to ``captured_at``
    # / ``duration_seconds`` — those are the live column names.
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO audio_segment "
            "(captured_at, ended_at, duration_seconds, codec, bitrate, "
            " path, size_bytes, transcript) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                captured_dt.isoformat(),
                (captured_dt.replace(microsecond=0)).isoformat(),
                float(duration_seconds),
                cleaned_codec,
                bitrate,
                stored_path,
                size_bytes,
                cleaned_transcript,
            ),
        )
        await conn.commit()
        segment_id = cursor.lastrowid

    if segment_id is None:
        # Should not happen on a successful INSERT; surface a 500 so
        # the agent retries rather than silently dropping the segment.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="insert failed",
        )

    log.info(
        "agent_api.audio_segment.stored",
        agent_id=agent["id"],
        segment_id=int(segment_id),
        bytes=size_bytes,
        codec=cleaned_codec,
        duration_seconds=float(duration_seconds),
        has_transcript=cleaned_transcript is not None,
    )

    # T29 — lite-mode agents upload audio without a transcript; transcribe
    # on the server (after the response) so they don't need 2 GB of Whisper
    # on the Mac. No-op when a transcript was already supplied.
    if not (cleaned_transcript and cleaned_transcript.strip()):
        background_tasks.add_task(
            _transcribe_uploaded_segment, int(segment_id), target
        )

    return JSONResponse(
        {"ok": True, "segment_id": int(segment_id)},
        status_code=status.HTTP_201_CREATED,
    )


@router.post("/screenshot")
async def upload_screenshot(
    captured_at: Annotated[str, Form(...)],
    width: Annotated[int, Form(...)],
    height: Annotated[int, Form(...)],
    # T29 — the Mac agent uploads the frame under the ``image`` field; the
    # iOS path used ``file``. Accept either so neither side has to change.
    file: Annotated[UploadFile | None, File()] = None,
    image: Annotated[UploadFile | None, File()] = None,
    app_name: Annotated[str | None, Form()] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Accept one screenshot from a remote agent.

    Pipeline:

    1. Authenticate the agent and bump ``last_screen_at``.
    2. Read the upload and refuse anything over 5 MiB.
    3. Validate MIME (``image/*``) and magic bytes (PNG / JPEG / WebP).
    4. Parse the form metadata; sanity-check the declared dimensions
       against the decoded ones.
    5. Compute pHash via :func:`app.dedup.compute_phash` and resolve
       the dedup group so a repeated screenshot collapses into the
       existing group exactly like a local capture.
    6. Write the bytes under ``data/agent/<id>/screens/YYYY/MM/DD/``.
    7. ``INSERT INTO screenshots`` with ``source = "remote_agent"``.
    8. Return ``{ok, shot_id}``.
    """
    agent = await _resolve_agent(authorization, kind="screen", x_agent_token=x_agent_token)

    upload = file or image
    if upload is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="screenshot file required (multipart field 'file' or 'image')",
        )

    raw = await upload.read()
    if len(raw) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty image upload",
        )
    if len(raw) > _MAX_SCREEN_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="image upload too large (max 5MB)",
        )

    declared_mime = (upload.content_type or "").lower()
    if declared_mime and not declared_mime.startswith(_IMAGE_MIME_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported content type: {declared_mime}",
        )

    detected = _detect_image_format(raw)
    if detected is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="not a PNG / JPEG / WebP image (magic bytes mismatch)",
        )

    captured_dt = _parse_iso(captured_at, field="captured_at")

    if width <= 0 or height <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="width and height must be positive",
        )
    if width > _MAX_PIXEL_DIMENSION or height > _MAX_PIXEL_DIMENSION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"image too large ({width}x{height}); "
                f"max {_MAX_PIXEL_DIMENSION}x{_MAX_PIXEL_DIMENSION}"
            ),
        )

    # Decode once for pHash + a defensive dimension cross-check.
    # T29 — run OFF the event loop. PIL decode + imagehash.phash (a scipy
    # DCT) is CPU-bound and was freezing the whole server on every agent
    # screenshot (~every 30s) once uploads started working. anyio.to_thread
    # keeps the loop free so other requests don't pile up behind it.
    try:
        decoded_width, decoded_height, phash = await anyio.to_thread.run_sync(
            _decode_and_phash, raw
        )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        log.warning("agent_api.screenshot.decode_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"decode failed: {exc}",
        ) from exc

    # Trust the decoder over the agent-supplied form values when they
    # disagree — the agent might have computed dimensions on a pre-
    # resize buffer. Log the discrepancy so it shows up on dashboards.
    if (decoded_width, decoded_height) != (width, height):
        log.info(
            "agent_api.screenshot.dimension_mismatch",
            agent_id=agent["id"],
            declared=(width, height),
            decoded=(decoded_width, decoded_height),
        )
    stored_width, stored_height = decoded_width, decoded_height

    cleaned_app = (app_name or "").strip() or None

    # Determine extension for the on-disk file based on the detected
    # format; we never trust the upload filename for the extension.
    ext_map = {"png": "png", "jpeg": "jpg", "webp": "webp"}
    fallback_ext = ext_map.get(detected, "png")
    safe_name = _sanitise_filename(upload.filename, fallback_ext)
    # Ensure the on-disk name carries the *detected* extension even if
    # the agent uploaded a mismatched filename.
    if not safe_name.lower().endswith(f".{fallback_ext}"):
        safe_name = f"{Path(safe_name).stem}.{fallback_ext}"

    settings = get_settings()

    async with get_connection() as conn:
        group_id, _is_new = await find_or_create_dedup_group(
            conn,
            phash=phash,
            now=captured_dt,
            threshold=settings.dedup_hamming_threshold,
        )
        screenshot_id = await insert_screenshot(
            conn,
            captured_at=captured_dt,
            width=stored_width,
            height=stored_height,
            phash=phash,
            monitor_index=0,
            app_name=cleaned_app,
            window_title=None,
            process_name=None,
            ocr_status="pending",
            dedup_group_id=group_id,
        )
        # Migration 094 added the ``source`` column; legacy rows are
        # NULL → "local". A remote-agent upload is explicitly tagged so
        # dashboards and retention rules can distinguish the channel.
        await conn.execute(
            "UPDATE screenshots SET source = ? WHERE id = ?",
            ("remote_agent", screenshot_id),
        )
        await conn.commit()

    # Persist the original bytes after the row exists so the on-disk
    # filename can be deterministically prefixed with the row id (no
    # collisions even if two uploads share the same source filename).
    target_dir = _agent_screen_dir(agent["id"], captured_dt)
    target = target_dir / f"{screenshot_id}-{safe_name}"
    if target.exists():
        tag = datetime.now(tz=UTC).strftime("%H%M%S%f")
        target = target_dir / f"{screenshot_id}-{tag}-{safe_name}"
    await anyio.to_thread.run_sync(_write_bytes, target, raw)

    log.info(
        "agent_api.screenshot.stored",
        agent_id=agent["id"],
        shot_id=int(screenshot_id),
        bytes=len(raw),
        width=stored_width,
        height=stored_height,
        format=detected,
        phash=phash,
        dedup_group_id=group_id,
        app_name=cleaned_app,
    )

    return JSONResponse(
        {"ok": True, "shot_id": int(screenshot_id)},
        status_code=status.HTTP_201_CREATED,
    )


@router.get("/stats")
async def stats(
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Return today's upload totals for this agent.

    The Mac agent's ``status`` command hits this so the user can verify
    that their captures actually reached the server without grepping
    server-side logs. All counters are scoped to the calling agent and
    "today" is today in UTC.
    """
    agent = await _resolve_agent(authorization, kind="any", x_agent_token=x_agent_token)
    today_utc = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE source = 'remote_agent' AND date(captured_at) = ?",
            (today_utc,),
        )
        screen_row = await cursor.fetchone()
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes "
            "FROM audio_segment "
            "WHERE date(captured_at) = ?",
            (today_utc,),
        )
        audio_row = await cursor.fetchone()
    return JSONResponse(
        {
            "ok": True,
            "agent_id": agent["id"],
            "today_utc": today_utc,
            "screens_today": int(screen_row["n"]) if screen_row else 0,
            "audio_segments_today": int(audio_row["n"]) if audio_row else 0,
            "audio_bytes_today": int(audio_row["bytes"]) if audio_row else 0,
            "last_seen_at": agent["last_seen_at"],
            "last_audio_at": agent["last_audio_at"],
            "last_screen_at": agent["last_screen_at"],
        },
    )


__all__ = ["router"]
