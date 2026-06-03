"""Generate PNG QR codes for share links.

Persona's v0.43 share-link UI lets an operator hand a signed, time-limited
URL to someone outside the workspace. On a desk-to-phone hand-off the
fastest path is a QR code on the admin page, scanned by the recipient's
camera — no copy/paste, no manual typing, no leaking the URL through a
chat app.

This module is the single-responsibility encoder behind that flow. The
public entry point :func:`make_qr_png` is **synchronous on purpose**:

* The route handler offloads it to a worker thread via
  :func:`anyio.to_thread.run_sync`, keeping the event loop free.
* CLI callers and tests can use it directly without spinning up an event
  loop just to encode a few hundred bytes.

The third-party ``qrcode`` package is an *optional* runtime dependency:
it is not listed in ``pyproject.toml`` because Persona's core capture +
search loop does not need it. We therefore import it lazily and, when
absent, fall back to a 1x1 transparent PNG so the route stays a 200
(rather than a 500 the operator cannot fix). The fallback is logged once
per call at ``warning`` level so a missing dep is visible in structured
logs without crashing the admin page.
"""

from __future__ import annotations

import io
from typing import Any

from app.logging_setup import get_logger

log = get_logger("persona.qr")


# 1x1 transparent RGBA PNG. Structure: 8-byte signature + IHDR + IDAT
# (a single zero-filtered, zero-RGBA pixel, zlib-compressed) + IEND.
# CRCs are precomputed; the round-trip was verified with Pillow at
# authoring time so callers always get a well-formed image/png body.
_STUB_PNG: bytes = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0bIDATx\x9cc`\x00\x02\x00\x00\x05\x00\x01z^\xab?"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_qr_png(text: str, box_size: int = 8) -> bytes:
    """Encode ``text`` as a PNG QR code and return the raw bytes.

    ``box_size`` is the pixel size of one QR module; the default of ``8``
    yields a roughly 200-300 px image for a typical share URL — large
    enough to scan reliably with a phone held at arm's length.

    When the optional ``qrcode`` package is not installed we log a
    warning and return :data:`_STUB_PNG`, a 1x1 transparent placeholder.
    The caller (route, template) still gets a valid ``image/png`` body
    and the page does not break; the operator simply sees a tiny dot
    where the QR ought to be — a clear, recoverable hint that
    ``uv pip install qrcode`` is needed.
    """
    try:
        import qrcode  # noqa: PLC0415
    except ImportError:
        log.warning("qr.qrcode_missing", text_len=len(text), fallback="stub_png")
        return _STUB_PNG

    qr: Any = qrcode.QRCode(box_size=box_size, border=2)
    qr.add_data(text)
    qr.make(fit=True)

    image: Any = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
