"""Per-shot privacy mask regions — list / add / remove / render redacted PNG.

A privacy mask is a black-filled rectangle that the user paints over a
sensitive region of a screenshot (API token, home address, payment
amount) before sharing or exporting. The rectangles are persisted in
``shot_privacy_mask`` (see migration ``133_privacy_mask.sql``) and
applied on demand by :func:`render_masked_image`, which loads the
original thumbnail from disk via Pillow, paints every rectangle in
solid black, and returns the resulting PNG bytes.

Design notes:

* Coordinates are stored in the original thumbnail's pixel space. The
  HTML editor maps mouse-drag events from the rendered canvas back into
  thumbnail-native pixels via the natural-vs-rendered ratio, so the
  rectangles render at the same logical location regardless of the
  viewport. We deliberately do not store the editor's display size —
  the underlying pixels are the only source of truth.
* :func:`render_masked_image` returns ``None`` when there are no masks
  yet (the caller should fall back to the unmasked thumbnail URL) or
  when the source file cannot be opened (deleted, antivirus-quarantined,
  retention pruned). This mirrors the contract used by
  :mod:`app.thumb_regen` for missing artifacts.
* All filesystem + Pillow work runs inside :func:`anyio.to_thread.run_sync`
  so the calling coroutine never blocks the event loop on disk IO.
* Pillow is declared in ``pyproject.toml`` already (used by the capture
  pipeline + dashboard cards). We still guard the import so a broken
  install degrades gracefully — ``render_masked_image`` returns
  ``None`` and the route degrades to 503.
* Every helper opens its own connection via
  :func:`app.storage.db.get_connection`, matching the call style used
  by :mod:`app.shot_reactions` and :mod:`app.bulk_favourite`. SQL is
  fully parametrised; there's no string interpolation against user
  input.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Final

import anyio

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.shot_privacy_masks")

# Pillow is shipped with Persona's runtime image (used by the capture
# pipeline + every PNG-card endpoint). The guarded import keeps the
# helper importable even if the install is broken — render_masked_image
# will return None and the HTTP route degrades to 503.
try:
    from PIL import Image, ImageDraw

    _PIL_AVAILABLE_INIT = True
except ImportError:  # pragma: no cover - exercised only when PIL absent
    _PIL_AVAILABLE_INIT = False

_PIL_AVAILABLE: Final[bool] = _PIL_AVAILABLE_INIT

# Solid black is the only colour we ever paint with — a mask is, by
# definition, a redaction, not a stylised overlay.
_MASK_FILL: Final[tuple[int, int, int]] = (0, 0, 0)


async def add_mask(
    shot_id: int,
    x: int,
    y: int,
    width: int,
    height: int,
    label: str | None = None,
) -> int:
    """Insert one privacy-mask rectangle for ``shot_id`` and return its id.

    Coordinates are in original-thumbnail pixel space (see module
    docstring). Width and height are clamped to a floor of ``1`` so a
    zero-area rectangle — easy to produce with an accidental mouse
    click without drag — does not poison the dataset with invisible
    rows.
    """
    safe_x = max(0, int(x))
    safe_y = max(0, int(y))
    safe_w = max(1, int(width))
    safe_h = max(1, int(height))
    safe_label = label if label else None

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO shot_privacy_mask "
            "(screenshot_id, x, y, width, height, label) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (shot_id, safe_x, safe_y, safe_w, safe_h, safe_label),
        )
        await conn.commit()
        new_id = int(cursor.lastrowid or 0)

    log.info(
        "shot_privacy_masks.added",
        mask_id=new_id,
        screenshot_id=shot_id,
        x=safe_x,
        y=safe_y,
        width=safe_w,
        height=safe_h,
        label=safe_label,
    )
    return new_id


async def remove_mask(mask_id: int) -> None:
    """Delete a single privacy-mask row by its primary key.

    No-op if the row is already gone — the editor double-firing a
    DELETE on a stale id should not 500. The structlog event always
    fires so the operator can see which ids were targeted.
    """
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM shot_privacy_mask WHERE id = ?",
            (int(mask_id),),
        )
        await conn.commit()

    log.info("shot_privacy_masks.removed", mask_id=int(mask_id))


async def list_masks_for_shot(shot_id: int) -> list[dict[str, Any]]:
    """Return every privacy-mask row for ``shot_id``, oldest first.

    Each dict carries ``id``, ``screenshot_id``, ``x``, ``y``,
    ``width``, ``height``, ``label`` and ``created_at`` — the same
    shape the HTTP route returns as JSON and the same shape the editor
    template iterates over for the per-row Delete buttons.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, screenshot_id, x, y, width, height, label, created_at "
            "FROM shot_privacy_mask "
            "WHERE screenshot_id = ? "
            "ORDER BY id ASC",
            (int(shot_id),),
        )
        rows = await cursor.fetchall()

    return [
        {
            "id": int(row[0]),
            "screenshot_id": int(row[1]),
            "x": int(row[2]),
            "y": int(row[3]),
            "width": int(row[4]),
            "height": int(row[5]),
            "label": (str(row[6]) if row[6] is not None else None),
            "created_at": str(row[7]),
        }
        for row in rows
    ]


async def _load_thumbnail_path(shot_id: int) -> str | None:
    """Return the ``thumbnail_path`` column for ``shot_id`` or ``None``."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT thumbnail_path FROM screenshots WHERE id = ?",
            (int(shot_id),),
        )
        row = await cursor.fetchone()

    if row is None:
        return None
    value = row[0]
    if value is None:
        return None
    return str(value)


def _render_png_sync(
    source_path: Path,
    masks: list[dict[str, Any]],
) -> bytes | None:
    """Paint every mask in solid black on top of ``source_path``; return PNG bytes.

    Runs in a worker thread via :func:`anyio.to_thread.run_sync`. Returns
    ``None`` when the source file cannot be opened — the caller turns
    that into a graceful "nothing to share" response rather than a 500.
    """
    if not _PIL_AVAILABLE:  # pragma: no cover - exercised only when PIL absent
        log.error("shot_privacy_masks.pillow_missing")
        return None

    try:
        with Image.open(source_path) as base:
            # ``base`` may be a palette-mode WebP — convert to RGB so
            # ImageDraw can write opaque black without alpha surprises.
            canvas = base.convert("RGB")
    except (OSError, ValueError) as exc:
        log.warning(
            "shot_privacy_masks.source_unreadable",
            path=str(source_path),
            error=str(exc),
        )
        return None

    draw = ImageDraw.Draw(canvas)
    for mask in masks:
        x0 = int(mask["x"])
        y0 = int(mask["y"])
        x1 = x0 + int(mask["width"])
        y1 = y0 + int(mask["height"])
        draw.rectangle((x0, y0, x1, y1), fill=_MASK_FILL)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def render_masked_image(shot_id: int) -> bytes | None:
    """Render the safe-to-share PNG for ``shot_id``.

    Returns ``None`` when:

    * there are no masks yet (caller should serve the unmasked
      thumbnail instead — there's nothing to redact);
    * the screenshot row has no ``thumbnail_path`` (already remediated
      by :mod:`app.thumb_regen` to ``NULL``);
    * the underlying file cannot be opened by Pillow (deleted,
      quarantined, permissions).

    Otherwise returns the binary PNG bytes with every mask painted in
    solid black on top of the original thumbnail.
    """
    masks = await list_masks_for_shot(shot_id)
    if not masks:
        log.info("shot_privacy_masks.render_skip_no_masks", screenshot_id=shot_id)
        return None

    thumb_str = await _load_thumbnail_path(shot_id)
    if thumb_str is None:
        log.info(
            "shot_privacy_masks.render_skip_no_source",
            screenshot_id=shot_id,
        )
        return None

    source_path = Path(thumb_str)
    png_bytes = await anyio.to_thread.run_sync(
        _render_png_sync,
        source_path,
        masks,
    )

    if png_bytes is None:
        return None

    log.info(
        "shot_privacy_masks.rendered",
        screenshot_id=shot_id,
        mask_count=len(masks),
        bytes=len(png_bytes),
    )
    return png_bytes


__all__ = [
    "add_mask",
    "list_masks_for_shot",
    "remove_mask",
    "render_masked_image",
]
