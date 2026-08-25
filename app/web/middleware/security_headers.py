"""Security response headers (CSP, nosniff, Referrer-Policy, frame denial, HSTS).

Before this module there was **no CSP anywhere** in Persona — the only mention
of the header was a comment in :mod:`app.web.routes.shot_embed` explaining why
that one route wants to be frameable. This middleware adds the baseline set.

Scope rules
-----------
* ``X-Content-Type-Options: nosniff`` goes on **every** response — it is free
  and a MIME-confusion attack on a JSON/API response is just as real as on a
  page.
* ``Content-Security-Policy`` goes on **HTML responses only**. Applying a
  policy to ``/static/sw.js`` would govern the service worker's own ``fetch``
  calls (it proxies cross-origin GETs — see ``static/sw.js:126``) and could
  silently break Google Fonts / Metrika under the SW. Non-HTML responses do not
  need a document policy.
* ``Strict-Transport-Security`` only when the request actually arrived over
  HTTPS (direct TLS or ``X-Forwarded-Proto: https`` from the reverse proxy).
  Sending HSTS over plain http:// is ignored by browsers and pinning a
  localhost dev origin to HTTPS would be actively harmful.
* Existing headers set by a route are never overwritten — see
  :data:`_FRAME_EXEMPT_SUFFIX` and the ``setdefault`` semantics below. That is
  what keeps ``/screenshot/{id}/embed`` (``X-Frame-Options: ALLOWALL``) working.

Why the CSP looks the way it does
---------------------------------
The policy below was derived from an inventory of what the templates and
static bundles actually load, not from a template. Every source is justified:

``default-src 'self'``
    Floor for any directive not named explicitly (``manifest-src``,
    ``prefetch-src``, …). Everything Persona serves is same-origin.

``script-src 'self' 'unsafe-inline' 'unsafe-eval' <yandex>``
    * ``'self'`` — all vendor bundles are **self-hosted** under
      ``/static/vendor/`` (tailwind-play, three, htmx, alpine, markdown-it,
      highlight, gsap, marked, sortable). No script CDN is used anywhere.
    * ``'unsafe-inline'`` — ~128 executable inline ``<script>`` blocks plus
      ~100 inline ``on*=`` handlers across 79 templates. A nonce cannot cover
      ``onclick=`` attributes, and adding a nonce would *disable* the
      ``'unsafe-inline'`` fallback that htmx's ``hx-on::`` handlers rely on.
      Removing this is the single biggest CSP win still available — it is what
      the Report-Only policy measures.
    * ``'unsafe-eval'`` — the shipped Alpine build (``alpine-3.14.7.min.js``)
      is the standard one, which compiles ``x-data``/``x-on`` expressions via
      the ``AsyncFunction`` constructor; htmx 2.0.4 uses ``new Function`` for
      ``hx-vals``/event filters. 48 templates use ``x-data``. Dropping this
      needs the ``@alpinejs/csp`` build and a rewrite of every expression.
    * ``https://mc.yandex.ru https://mc.yandex.com https://yastatic.net`` —
      Yandex.Metrika (``templates/_metrika.html``), injected as a dynamic
      ``<script src>``. Both ``.ru`` and ``.com`` are listed because the tag
      switches domain by region; ``yastatic.net`` serves the Webvisor player.
    * NOT allowed: ``'wasm-unsafe-eval'``. Verified there is no WebAssembly in
      first-party code or vendor bundles (the only ``WebAssembly`` string is a
      highlight.js *grammar name*).

``style-src 'self' 'unsafe-inline' https://fonts.googleapis.com``
    448 inline ``style="…"`` attributes across 94 templates, 39 ``<style>``
    blocks, **and** ``vendor/tailwind-play.js`` — the Tailwind Play JIT, which
    injects a generated ``<style>`` element at runtime on every page. Alpine's
    ``x-transition``/``x-show`` also writes ``element.style`` directly.
    ``'unsafe-inline'`` is unavoidable until Tailwind is precompiled.
    ``fonts.googleapis.com`` serves the landing page's Space Grotesk CSS.

``font-src 'self' data: https://fonts.gstatic.com``
    ``fonts.gstatic.com`` is where the Google CSS points its ``@font-face``
    ``src``. ``data:`` is defensive (no data: font is used today, but an
    inlined icon font is a one-line change that would otherwise 404 silently).

``img-src 'self' data: blob: https:``
    ``data:`` — inline SVG backgrounds in ``static/landing/style.css`` and
    ``static/landing_v2/style.css``, plus base64 chat image attachments.
    ``blob:`` — ``URL.createObjectURL`` thumbnails (``screenshot.html:816``).
    ``https:`` — deliberately broad. Blog/marketing content and the Metrika
    tracking pixel pull images from arbitrary hosts, and an over-tight
    ``img-src`` is the classic way a CSP silently breaks user content. Images
    cannot execute code; the exfiltration channel an ``img-src`` restriction
    would close is not meaningfully narrower than the one already open through
    ``script-src 'unsafe-inline'``, so tightening it here buys close to nothing
    while risking visible breakage.

``connect-src 'self' http://127.0.0.1:8770 <yandex http+wss>``
    ``'self'`` covers the SSE streams (``/events``,
    ``/api/notifications/stream``) and every fetch/htmx call. The only
    off-origin XHR in first-party code is
    ``fetch('http://127.0.0.1:8770/api/load/now')`` in ``chat_index.html`` (the
    local PC resource-load probe) — a different *origin* from the app even on
    localhost, so it needs an explicit entry. Metrika Webvisor beacons use both
    HTTPS XHR and a WebSocket, hence the ``wss://`` entries (``https://`` in
    CSP does **not** match ``wss://``).

``media-src 'self' blob:``
    Voice capture builds ``Blob``s (ask / chat / journal / voice pages). They
    are POSTed today, but local playback of a recording is one line away.

``worker-src 'self'``
    ``navigator.serviceWorker.register('/static/sw.js')`` in ``base.html``.
    No ``new Worker`` / ``SharedWorker`` exists in first-party code.

``frame-src 'self' <yandex>``
    Same-origin widget iframes (``/widget/mobile-bottom-nav``) and the hidden
    Metrika sync frame.

``object-src 'none'``, ``base-uri 'self'``, ``form-action 'self'``
    Verified: no ``<object>``/``<embed>``, no ``<base>``, and no form action
    pointing off-origin anywhere in the templates. These three are enforced
    with high confidence and are exactly the directives that stop
    ``<base href=evil>`` hijacking and POST-exfiltration of a form.

``frame-ancestors 'self'``
    Clickjacking. Paired with ``X-Frame-Options: SAMEORIGIN`` for old engines.
    **Exempt:** ``/screenshot/{id}/embed`` sets ``X-Frame-Options: ALLOWALL``
    on purpose; ``frame-ancestors`` would override XFO in every modern browser
    and silently kill the public embed. That route is detected via
    :data:`_FRAME_EXEMPT_SUFFIX` and gets ``frame-ancestors *`` instead.

Report-Only
-----------
A second header, ``Content-Security-Policy-Report-Only``, ships the *target*
policy: ``script-src 'self'`` and ``style-src 'self'`` with no ``unsafe-*``.
It blocks nothing; every violation it prints in the browser console is one item
on the "what must change before we can enforce" list. Turn it off with kv
``csp_report_only=0`` if the console noise is in the way.

Fail-safe
---------
The policy strings are module constants — no I/O, no DB, nothing that can
raise on the hot path. The kv toggles are read through a 60 s cache and any
failure keeps the **more secure** default (headers still sent).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import proxies as _proxies
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

log = get_logger("persona.security_headers")

__all__ = [
    "CSP_ENFORCED",
    "CSP_REPORT_ONLY",
    "PERMISSIONS_POLICY_DEFAULT",
    "PERMISSIONS_POLICY_MIC",
    "SecurityHeadersMiddleware",
]

# Routes that opt out of frame denial. ``/screenshot/{id}/embed`` is public by
# design (see app.web.routes.shot_embed).
_FRAME_EXEMPT_SUFFIX = "/embed"
_FRAME_EXEMPT_PREFIX = "/screenshot/"

_YANDEX_SCRIPT = "https://mc.yandex.ru https://mc.yandex.com https://yastatic.net"
_YANDEX_CONNECT = (
    "https://mc.yandex.ru https://mc.yandex.com https://yastatic.net "
    "https://*.yandex.net wss://mc.yandex.ru wss://mc.yandex.com"
)
_YANDEX_IMG = "https://mc.yandex.ru https://mc.yandex.com"
_YANDEX_FRAME = "https://mc.yandex.ru https://mc.yandex.com"


def _policy(*, frame_ancestors: str) -> str:
    """Assemble the enforced policy with a caller-chosen frame-ancestors."""
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'unsafe-inline' 'unsafe-eval' {_YANDEX_SCRIPT}",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' data: https://fonts.gstatic.com",
            f"img-src 'self' data: blob: https: {_YANDEX_IMG}",
            f"connect-src 'self' http://127.0.0.1:8770 {_YANDEX_CONNECT}",
            "media-src 'self' blob:",
            "worker-src 'self'",
            "manifest-src 'self'",
            f"frame-src 'self' {_YANDEX_FRAME}",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            f"frame-ancestors {frame_ancestors}",
        )
    )


#: Policy sent on ordinary HTML pages.
CSP_ENFORCED = _policy(frame_ancestors="'self'")

#: Policy sent on the deliberately-iframable embed route.
CSP_ENFORCED_EMBEDDABLE = _policy(frame_ancestors="*")

#: The target policy. Blocks nothing; each console violation is a TODO.
CSP_REPORT_ONLY = "; ".join(
    (
        "default-src 'self'",
        f"script-src 'self' {_YANDEX_SCRIPT}",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        f"img-src 'self' data: blob: {_YANDEX_IMG}",
        f"connect-src 'self' {_YANDEX_CONNECT}",
        "media-src 'self' blob:",
        "worker-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    )
)

# camera/geolocation/payment/usb/… are denied outright: nothing in Persona uses
# them, and an injected script inheriting them is pure downside. ``microphone``
# is denied by default and re-allowed as ``(self)`` only on the pages that call
# ``getUserMedia``/SpeechRecognition — note that ``(self)`` does not *grant*
# anything, the browser still shows its own permission prompt.
_DENIED_FEATURES = (
    "accelerometer=()",
    "autoplay=(self)",
    "camera=()",
    "display-capture=()",
    "encrypted-media=()",
    "fullscreen=(self)",
    "geolocation=()",
    "gyroscope=()",
    "magnetometer=()",
    "midi=()",
    "payment=()",
    "picture-in-picture=(self)",
    "publickey-credentials-get=(self)",
    "screen-wake-lock=()",
    "serial=()",
    "usb=()",
    "xr-spatial-tracking=()",
)

PERMISSIONS_POLICY_DEFAULT = ", ".join(("microphone=()", *_DENIED_FEATURES))
PERMISSIONS_POLICY_MIC = ", ".join(("microphone=(self)", *_DENIED_FEATURES))

# Pages whose templates call getUserMedia / SpeechRecognition:
#   /chat            chat_index.html          (voice dictation in the composer)
#   /voice           voice_chat.html          (voice assistant)
#   /ask             ask.html                 (ask-by-voice)
#   /journal/voice   journal_voice.html       (voice diary)
#   /search/voice    voice_search.html        (voice search)
#   /widget/voice-note  _voice_note_widget.html (HTMX record-button fragment)
_MIC_PREFIXES: tuple[str, ...] = (
    "/chat",
    "/voice",
    "/ask",
    "/journal/voice",
    "/search/voice",
    "/widget/voice-note",
)

# 1 year, subdomains included. No ``preload``: submitting to the HSTS preload
# list is a one-way door for a domain that may still need a plain-http
# subdomain, and it is the owner's call, not a middleware's.
HSTS_VALUE = "max-age=31536000; includeSubDomains"

_KV_TTL = 60.0
_kv_cache: dict[str, object] = {"value": None, "checked_at": 0.0}


def _matches_mic_page(path: str) -> bool:
    if ".." in path:
        return False
    return any(path == p or path.startswith(p + "/") for p in _MIC_PREFIXES)


def _is_frame_exempt(path: str) -> bool:
    """True for ``/screenshot/{id}/embed`` — the one intentionally-framed page."""
    return path.startswith(_FRAME_EXEMPT_PREFIX) and path.endswith(_FRAME_EXEMPT_SUFFIX)


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if forwarded:
        return forwarded.lower() == "https"
    return request.url.scheme == "https"


async def _report_only_enabled() -> bool:
    """kv ``csp_report_only`` (default ON). Failure keeps the default."""
    now = time.monotonic()
    cached = _kv_cache["value"]
    if cached is not None and now - float(_kv_cache["checked_at"]) < _KV_TTL:  # type: ignore[arg-type]
        return bool(cached)
    enabled = True
    try:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv  # noqa: PLC0415

        async with get_connection() as conn:
            raw = await get_kv(conn, "csp_report_only")
        if raw is not None and str(raw).strip() in {"0", "off", "false", "no"}:
            enabled = False
    except Exception as exc:  # noqa: BLE001 — a header toggle must never 500
        log.debug("csp.report_only_flag_failed", error=str(exc))
        enabled = True
    _kv_cache["value"] = enabled
    _kv_cache["checked_at"] = now
    return enabled


def reset_cache() -> None:
    """Drop the kv toggle cache (tests / after a settings change)."""
    _kv_cache["value"] = None
    _kv_cache["checked_at"] = 0.0


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the baseline security response headers.

    Placed **outermost** in the middleware stack so the headers land on every
    response, including the 303s and 401/403s produced by the auth gate and the
    429s produced by the throttle — a redirect to a login page is exactly the
    kind of response an attacker would like to frame.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Keep the trusted-proxy set warm. ``app.web.routes.auth._client_ip``
        # is synchronous (the billing webhook imports it as such) and therefore
        # cannot read kv itself; this is the one place on the request path that
        # runs for every request and can await. 60 s cached → ~free.
        await _proxies.prime()

        response = await call_next(request)
        path = request.url.path

        # nosniff on everything — including JSON and static assets.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        # Legacy IE/Edge XSS auditor. ``0`` is the modern recommendation: the
        # auditor itself was a vulnerability class and is gone from all current
        # engines; explicitly disabling it avoids the few remaining buggy ones.
        response.headers.setdefault("X-XSS-Protection", "0")

        if _is_https(request):
            response.headers.setdefault("Strict-Transport-Security", HSTS_VALUE)

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            return response

        frame_exempt = _is_frame_exempt(path)
        if frame_exempt:
            # The route already set X-Frame-Options: ALLOWALL. Do not add a
            # frame-ancestors that would override it.
            response.headers.setdefault(
                "Content-Security-Policy", CSP_ENFORCED_EMBEDDABLE
            )
        else:
            response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            response.headers.setdefault("Content-Security-Policy", CSP_ENFORCED)
            if await _report_only_enabled():
                response.headers.setdefault(
                    "Content-Security-Policy-Report-Only", CSP_REPORT_ONLY
                )

        response.headers.setdefault(
            "Permissions-Policy",
            PERMISSIONS_POLICY_MIC
            if _matches_mic_page(path)
            else PERMISSIONS_POLICY_DEFAULT,
        )
        return response
