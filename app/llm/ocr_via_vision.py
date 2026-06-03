"""OCR fallback via multimodal LLM — re-transcribe low-confidence shots.

The primary OCR pipeline (:mod:`app.ocr.tesseract`) sometimes produces
empty or sub-confidence output: stylised UI fonts, tiny CJK glyphs,
low-contrast dark-mode chrome. For those rows the user can — if they
have a multimodal BYO LLM provider configured (currently Anthropic
only) and have explicitly flipped ``PERSONA_LLM_VISION_ENABLED=1`` —
re-prompt the model with the thumbnail bytes as a base64 image and
ask it to read the text directly.

This module is the entry point for that flow. It:

* reads the shot row + thumbnail bytes (anyio.to_thread for the
  blocking file I/O so we don't peg the event loop),
* base64-encodes the bytes and POSTs a multimodal message to the
  Anthropic Messages API,
* caches the result in ``screenshots.ocr_text_vision`` so a second
  click never re-spends the user's API key budget,
* NEVER touches ``screenshots.ocr_text`` — Tesseract's column stays
  the canonical text source; vision is a sidecar.

Returned status codes follow the same vocabulary the rest of the LLM
feature surface uses (see :mod:`app.llm.day_tldr`):

* ``ok`` — text was extracted (either fresh or from cache).
* ``empty`` — vision succeeded but returned no readable text (cached
  as ``""`` so subsequent calls short-circuit).
* ``missing_config`` — feature is off, no BYO key, or the configured
  provider is not the multimodal one.
* ``low_provider`` — BYO provider is set but it isn't Anthropic.
* ``error`` — the shot has no thumbnail file, the model raised, or
  some other unexpected failure occurred.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

import anyio
import httpx

from app.llm.client import LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.ocr.vision")

Status = Literal["ok", "empty", "missing_config", "low_provider", "error"]


class VisionResult(TypedDict):
    status: Status
    text: str


# Prompt is deliberately terse: vision models reliably comply with
# "return only the text" — verbose system messages just inflate tokens.
_SYSTEM_PROMPT = (
    "You are a faithful OCR engine. The user will send one screenshot. "
    "Transcribe every visible piece of text in reading order. Preserve "
    "line breaks where they obviously separate UI elements. Do not "
    "summarise, do not describe images, do not add commentary."
)
_USER_PROMPT = (
    "Transcribe all visible text from this screenshot. Return only the "
    "text, no commentary."
)

# Anthropic Messages API endpoint. We POST directly rather than going
# through ``LLMClient.complete`` because the Protocol's signature only
# accepts text; the multimodal payload below is a parallel code path.
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# A WebP thumbnail at q=45 / max-width 900 lands well under this cap in
# practice (~50 KB typical), but Anthropic's documented per-image limit
# is 5 MB and the API rejects oversize images with a 400 — guard at the
# call site so we surface a clean ``error`` instead of a stack trace.
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _detect_media_type(path: Path) -> str:
    """Map a thumbnail file extension to its Anthropic media-type string.

    Persona writes WebP by default (see :mod:`app.storage.thumbnails`)
    but we tolerate PNG / JPEG too — older tier-warm conversions and
    user-supplied test fixtures occasionally land on those formats.
    """
    suffix = path.suffix.lower()
    if suffix == ".webp":
        return "image/webp"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".gif":
        return "image/gif"
    # Anthropic accepts the four types above; anything else gets an
    # explicit ``error`` status from the caller — but we still return a
    # sensible default so logs show something meaningful.
    return "application/octet-stream"


def _read_bytes_sync(path: Path) -> bytes:
    """Blocking ``Path.read_bytes`` — moved to a thread by the caller."""
    return path.read_bytes()


async def _read_thumbnail_bytes(thumbnail_path: str | None) -> bytes | None:
    """Read the thumbnail file off disk via ``anyio.to_thread``.

    Returns ``None`` if the row has no thumbnail or the file is
    missing — callers translate that into an ``error`` status because
    there's nothing for the model to read.
    """
    if not thumbnail_path:
        return None
    path = Path(thumbnail_path)
    if not path.exists() or not path.is_file():
        return None
    return await anyio.to_thread.run_sync(_read_bytes_sync, path)


async def _read_shot_row(
    conn: aiosqlite.Connection, shot_id: int
) -> tuple[str | None, str | None] | None:
    """Return ``(thumbnail_path, cached_vision_text)`` for ``shot_id``.

    Returns ``None`` when the row does not exist. ``cached_vision_text``
    is ``None`` when vision has never run for the shot, an empty string
    when the previous pass returned no text (cached negative), or the
    cached transcription otherwise.
    """
    cursor = await conn.execute(
        "SELECT thumbnail_path, ocr_text_vision FROM screenshots WHERE id = ?",
        (shot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    thumb = None if row["thumbnail_path"] is None else str(row["thumbnail_path"])
    cached_raw = row["ocr_text_vision"]
    cached = None if cached_raw is None else str(cached_raw)
    return thumb, cached


async def _cache_vision_text(
    conn: aiosqlite.Connection, shot_id: int, text: str
) -> None:
    """Persist ``text`` into ``screenshots.ocr_text_vision`` for ``shot_id``.

    Never touches ``ocr_text`` — Tesseract owns that column. Storing
    an empty string is meaningful: it caches a negative result so a
    second click on the admin button doesn't re-invoice the user.
    """
    await conn.execute(
        "UPDATE screenshots SET ocr_text_vision = ? WHERE id = ?",
        (text, shot_id),
    )
    await conn.commit()


def _build_payload(
    *,
    model: str,
    image_b64: str,
    media_type: str,
) -> dict[str, object]:
    """Assemble the Anthropic Messages multimodal payload."""
    return {
        "model": model,
        "max_tokens": 1500,
        "temperature": 0.0,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": _USER_PROMPT},
                ],
            }
        ],
    }


def _extract_text_blocks(data: dict[str, object]) -> str:
    """Return the concatenated text blocks from an Anthropic response.

    The API returns ``{"content": [{"type": "text", "text": ...}, ...]}``;
    we join the text blocks and strip the result. Returns an empty
    string when no text block is present.
    """
    content = data.get("content")
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        value = block.get("text")
        if isinstance(value, str):
            pieces.append(value)
    return "\n".join(pieces).strip()


async def _call_anthropic_vision(
    *,
    api_key: str,
    model: str,
    image_b64: str,
    media_type: str,
) -> str:
    """POST the multimodal payload to Anthropic. Returns extracted text.

    Raises ``httpx.HTTPError`` on transport / non-2xx responses — the
    caller wraps these into an ``error`` status. The API key only
    lives in the per-request ``headers`` dict; we never log it.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = _build_payload(model=model, image_b64=image_b64, media_type=media_type)
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(_ANTHROPIC_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        return ""
    return _extract_text_blocks(data)


def _gate_provider(provider: str) -> Status | None:
    """Translate the configured BYO provider into a gating status code.

    Returns ``None`` when the provider is acceptable (Anthropic). Any
    other configured provider returns ``"low_provider"``; an entirely
    unconfigured provider returns ``"missing_config"``.
    """
    normalised = provider.strip().lower()
    if normalised == "":
        return "missing_config"
    if normalised != "anthropic":
        return "low_provider"
    return None


def _check_gates(shot_id: int) -> VisionResult | None:
    """Return an early-exit ``VisionResult`` if any configuration gate fails.

    Bundles the three gating reasons (feature disabled, wrong provider,
    missing key) so the main coroutine doesn't blow ruff's return-count
    budget. Returns ``None`` when every gate passes — the caller can
    proceed with the actual LLM call.
    """
    settings = get_settings()

    if not settings.llm_vision_enabled:
        log.info("ocr.vision.gate.disabled", shot_id=shot_id)
        return {"status": "missing_config", "text": ""}

    provider_gate = _gate_provider(settings.byo_api_provider)
    if provider_gate is not None:
        log.info(
            "ocr.vision.gate.provider",
            shot_id=shot_id,
            status=provider_gate,
            provider=settings.byo_api_provider or "(unset)",
        )
        return {"status": provider_gate, "text": ""}

    try:
        make_client()
    except LLMNotConfigured:
        log.info("ocr.vision.gate.no_key", shot_id=shot_id)
        return {"status": "missing_config", "text": ""}

    return None


async def _load_image(
    shot_id: int, thumb_path: str | None
) -> tuple[bytes, str] | VisionResult:
    """Read the thumbnail bytes + detect media type.

    Returns ``(image_bytes, media_type)`` on success, or a fully-formed
    ``VisionResult`` carrying ``status='error'`` when the file is
    missing or too large for Anthropic's per-image cap. The split keeps
    the main coroutine's branching shallow.
    """
    image_bytes = await _read_thumbnail_bytes(thumb_path)
    if image_bytes is None:
        log.warning(
            "ocr.vision.no_thumbnail", shot_id=shot_id, path=thumb_path or "(null)"
        )
        return {"status": "error", "text": ""}
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        log.warning(
            "ocr.vision.image_too_large",
            shot_id=shot_id,
            bytes=len(image_bytes),
            cap=_MAX_IMAGE_BYTES,
        )
        return {"status": "error", "text": ""}
    media_type = _detect_media_type(Path(thumb_path or ""))
    return image_bytes, media_type


async def extract_text_via_vision(shot_id: int) -> VisionResult:
    """Re-transcribe the shot's thumbnail via the BYO multimodal LLM.

    Idempotent: if a previous pass already cached a result (including a
    cached negative — empty string), the cached value is returned with
    no API call. The Tesseract-owned ``ocr_text`` column is NEVER
    written to from this code path.

    The function is intentionally tolerant: configuration problems and
    transient errors return a status string instead of raising, so the
    route layer can render a meaningful message without try / except
    plumbing.
    """
    gate = _check_gates(shot_id)
    if gate is not None:
        return gate

    settings = get_settings()
    provider_label = "anthropic"

    async with get_connection() as conn:
        row = await _read_shot_row(conn, shot_id)
        if row is None:
            log.warning("ocr.vision.missing_shot", shot_id=shot_id)
            return {"status": "error", "text": ""}
        thumb_path, cached = row

        # Cache hit (including cached negative). Either way, no API call.
        if cached is not None:
            log.info(
                "ocr.vision.cache.hit",
                shot_id=shot_id,
                provider=provider_label,
                length=len(cached),
            )
            status: Status = "ok" if cached.strip() else "empty"
            return {"status": status, "text": cached}

        loaded = await _load_image(shot_id, thumb_path)
        if isinstance(loaded, dict):
            return loaded
        image_bytes, media_type = loaded
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")

        # Reuse the AnthropicClient's default model so any future model
        # bump in ``app.llm.client`` carries over here without a
        # duplicate constant. ``make_client`` is cheap — the key was
        # already validated by ``_check_gates``.
        client = make_client()
        model = getattr(client, "_model", "claude-haiku-4-5-20251001")

        log.info(
            "ocr.vision.call.start",
            shot_id=shot_id,
            provider=provider_label,
            model=model,
            media_type=media_type,
            bytes=len(image_bytes),
        )
        try:
            text = await _call_anthropic_vision(
                api_key=settings.byo_api_key,
                model=model,
                image_b64=image_b64,
                media_type=media_type,
            )
        except httpx.HTTPError as exc:
            log.warning(
                "ocr.vision.call.http_error",
                shot_id=shot_id,
                provider=provider_label,
                error=str(exc),
            )
            return {"status": "error", "text": ""}

        cleaned = text.strip()
        await _cache_vision_text(conn, shot_id, cleaned)
        log.info(
            "ocr.vision.call.done",
            shot_id=shot_id,
            provider=provider_label,
            chars=len(cleaned),
        )
        final_status: Status = "ok" if cleaned else "empty"
        return {"status": final_status, "text": cleaned}
