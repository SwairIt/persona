"""CSRF protection for the cookie-authenticated surface.

Threat model — what SameSite=Lax already covers, and what it does not
--------------------------------------------------------------------
``persona_session`` is ``HttpOnly; SameSite=Lax``. Lax means the cookie is
**not** sent on cross-site ``POST``/``fetch``, so the classic "hidden form on
evil.com auto-submits to persona" attack is already dead in every current
browser. What Lax does **not** cover:

1. **Same-site attackers.** Any content served from the same registrable domain
   — a future ``blog.<domain>``, a user-content page, an XSS anywhere — is
   "same-site" for cookie purposes and can forge requests freely.
2. **Top-level GET navigations that change state.** Lax deliberately *does*
   send the cookie on a top-level GET. Any endpoint that mutates on GET is
   therefore CSRF-able by a plain ``<a href>`` or ``<img src>``. In Persona
   that was ``GET /auth/logout`` — now fixed to render a confirmation form
   instead of logging the user out (see ``app/web/routes/auth.py``).
3. **Browsers without Lax-by-default** (older Safari/iOS quirks, embedded
   webviews, anything driven by an automation stack that ignores SameSite).
4. **Method-override / CORS-simple-request tricks** on endpoints that accept
   ``text/plain`` bodies.

So SameSite is a strong first layer, not the whole answer.

Token scheme: derived (a.k.a. signed) double-submit
---------------------------------------------------
The token is **derived from the session token**::

    csrf = HMAC-SHA256(key=<session cookie value>, msg=b"persona-csrf-v1").hex()

Properties this buys over a plain random double-submit cookie:

* **Unforgeable without the session.** A same-site attacker who can *write*
  cookies (the standard double-submit weakness — cookies are not origin-scoped)
  still cannot produce a matching token, because the session cookie is
  ``HttpOnly`` and never leaves the browser's cookie jar for script.
* **No server-side storage.** Nothing to expire, no table, no cleanup job.
* **Automatic rotation.** Rotating the session token (login, password change —
  both do rotate now) rotates the CSRF token for free, so a token captured
  before a privilege change is dead after it.

The derived value is published in a **readable** cookie ``persona_csrf``
(``HttpOnly`` deliberately *off* — JS must read it to set the header) and is
accepted from any of:

* header ``X-CSRF-Token`` (fetch / htmx / Alpine — see ``static/csrf.js``);
* form field ``csrf_token`` (plain ``application/x-www-form-urlencoded`` forms);
* query parameter ``csrf_token`` (multipart uploads, where buffering the body
  in middleware would be a memory-exhaustion bug).

Rollout mode
------------
kv ``csrf_mode``:

``off``
    Do nothing but keep publishing the cookie.
``report`` (**default, and what ships today**)
    Validate, log ``csrf.violation`` at WARNING with method/path/reason, and
    **let the request through**. This exists because enforcing today would
    break 237 ``<form method=post>`` blocks, 35 ``hx-post`` attributes and 118
    ``fetch(..., {method:'POST'})`` call sites across templates this agent does
    not own. The log is the punch-list.
``enforce``
    Reject with ``403`` (JSON for ``/api/*``, a small HTML page otherwise).

Flip to ``enforce`` only after ``static/csrf.js`` is loaded from ``base.html``
and the forms listed in the handover have the hidden field. Until then the log
tells you exactly which endpoints would have broken.

Exemptions (never CSRF-checked)
-------------------------------
* Safe methods (``GET``/``HEAD``/``OPTIONS``/``TRACE``).
* Requests with no ``persona_session`` cookie — there is no ambient authority
  to abuse. (Login/registration POSTs land here; login-CSRF is a real but much
  lower-severity issue, and a token there would break the un-scripted form.)
* Requests carrying ``Authorization:`` — bearer-token auth is not ambient.
* :data:`_EXEMPT_PREFIXES` — machine callers that authenticate with their own
  header token and have no cookie session (agents, devices, the LLM worker, the
  ЮKassa webhook, license validation).

Fail-safe
---------
An unreadable kv value, a DB error, or an unparseable body all resolve to the
**most protective** outcome the current mode allows: in ``enforce`` a missing
or unverifiable token is a rejection, never a pass. The mode itself defaults to
``report`` on any error rather than ``off``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from app.auth.sessions import SESSION_COOKIE_NAME
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = get_logger("persona.csrf")

__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_FIELD_NAME",
    "CSRF_HEADER_NAME",
    "CsrfMiddleware",
    "csrf_input",
    "csrf_token_for_session",
    "reset_cache",
]

CSRF_COOKIE_NAME = "persona_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_FIELD_NAME = "csrf_token"

_DERIVATION_MSG = b"persona-csrf-v1"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Machine-to-machine surfaces: header/token authenticated, no cookie session.
# Mirrors the allow-list in app/web/middleware/auth_gate.py.
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/agent/",
    "/api/sync/",
    "/api/devices/",
    "/api/workspace/",
    "/api/ingest/",
    "/api/install/",
    "/api/voice/",
    "/api/alice/",
    "/api/llm/worker",
    "/api/audio/mic",
    "/api/v1/license",
    "/billing/webhook",
)

# Largest urlencoded body we will buffer to look for the form field. Above this
# (and for every multipart upload) the header or ?csrf_token= is required —
# buffering a 200 MB upload inside middleware is a denial-of-service, not a
# security control.
_MAX_BUFFER_BYTES = 512 * 1024

_KV_TTL = 60.0
_cache: dict[str, object] = {"value": None, "checked_at": 0.0}

_VALID_MODES = frozenset({"off", "report", "enforce"})
_DEFAULT_MODE = "report"


def reset_cache() -> None:
    """Drop the cached ``csrf_mode`` (tests / after a settings change)."""
    _cache["value"] = None
    _cache["checked_at"] = 0.0


async def _mode() -> str:
    """Resolve kv ``csrf_mode``. Unknown/unreadable → ``report`` (never ``off``)."""
    now = time.monotonic()
    cached = _cache["value"]
    if cached is not None and now - float(_cache["checked_at"]) < _KV_TTL:  # type: ignore[arg-type]
        return str(cached)
    mode = _DEFAULT_MODE
    try:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv  # noqa: PLC0415

        async with get_connection() as conn:
            raw = await get_kv(conn, "csrf_mode")
        candidate = str(raw or "").strip().lower()
        if candidate in _VALID_MODES:
            mode = candidate
    except Exception as exc:  # noqa: BLE001 — config failure keeps the default
        log.debug("csrf.mode_read_failed", error=str(exc))
        mode = _DEFAULT_MODE
    _cache["value"] = mode
    _cache["checked_at"] = now
    return mode


def csrf_token_for_session(session_token: str | None) -> str:
    """Derive the CSRF token for a session token. Empty string when signed out."""
    if not session_token:
        return ""
    return hmac.new(
        session_token.encode("utf-8"), _DERIVATION_MSG, hashlib.sha256
    ).hexdigest()


def csrf_token_for_request(request: object) -> str:
    """Jinja/route helper: the CSRF token for the current request, or ``""``.

    Accepts anything with a ``cookies`` mapping (Starlette ``Request``).
    """
    cookies = getattr(request, "cookies", None) or {}
    return csrf_token_for_session(cookies.get(SESSION_COOKIE_NAME))


def csrf_input(request: object) -> str:
    """Jinja global: the hidden input to drop inside a ``<form method="post">``.

    Returns a ``markupsafe.Markup`` so Jinja renders it as HTML. Emits an empty
    string for an unauthenticated request (nothing to protect, and an empty
    hidden field would look like a bug in the DOM).
    """
    from markupsafe import Markup  # noqa: PLC0415 — keep import cost off startup

    token = csrf_token_for_request(request)
    # Belt and braces before building raw markup: the token is an HMAC-SHA256
    # hex digest by construction, so anything that is not 64 lowercase hex
    # characters means the derivation was tampered with and must not reach the
    # DOM unescaped.
    if not token or len(token) != 64 or not all(c in "0123456789abcdef" for c in token):
        return Markup("")
    return Markup(  # noqa: S704 — token verified above to be pure hex
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{token}">'
    )


def _cookie_header(token: str, secure: bool) -> str:
    flags = f"{CSRF_COOKIE_NAME}={token}; Path=/; SameSite=Lax; Max-Age=2592000"
    # Deliberately NOT HttpOnly: the whole point is that page JS reads it to
    # populate X-CSRF-Token. Its value is useless without the HttpOnly session
    # cookie it is derived from, so exposing it costs nothing.
    return flags + ("; Secure" if secure else "")


def _is_https(headers: dict[bytes, bytes], scheme: str) -> bool:
    forwarded = headers.get(b"x-forwarded-proto", b"").decode("latin-1")
    first = forwarded.split(",")[0].strip().lower()
    if first:
        return first == "https"
    return scheme == "https"


def _parse_cookies(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


class CsrfMiddleware:
    """Pure-ASGI CSRF middleware.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because it needs to (a) buffer
    and then **replay** a request body to find the form field without consuming
    it for the route, and (b) inject a ``Set-Cookie`` into the response start
    message. Both are awkward-to-impossible through the ``BaseHTTPMiddleware``
    request/response abstraction.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        cookies = _parse_cookies(headers.get(b"cookie", b"").decode("latin-1"))
        session_token = cookies.get(SESSION_COOKIE_NAME)
        expected = csrf_token_for_session(session_token)

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        if expected and method not in _SAFE_METHODS and not self._exempt(path, headers):
            mode = await _mode()
            if mode != "off":
                receive, ok, reason = await self._verify(
                    scope, receive, headers, expected
                )
                if not ok:
                    log.warning(
                        "csrf.violation",
                        mode=mode,
                        method=method,
                        path=path,
                        reason=reason,
                    )
                    if mode == "enforce":
                        await self._reject(scope, send, path)
                        return

        await self.app(scope, receive, self._wrap_send(scope, headers, send, expected, cookies))

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _exempt(path: str, headers: dict[bytes, bytes]) -> bool:
        if headers.get(b"authorization"):
            # Bearer-authenticated: not ambient authority, no CSRF surface.
            return True
        return any(path.startswith(p) for p in _EXEMPT_PREFIXES)

    async def _verify(
        self,
        scope: Scope,
        receive: Receive,
        headers: dict[bytes, bytes],
        expected: str,
    ) -> tuple[Receive, bool, str]:
        """Return ``(receive-to-use-downstream, ok, reason)``.

        The returned ``receive`` replays any buffered body so the route still
        sees a complete request.
        """
        supplied = headers.get(CSRF_HEADER_NAME.encode("ascii"), b"").decode("latin-1")
        if supplied:
            ok = hmac.compare_digest(supplied, expected)
            return receive, ok, "ok" if ok else "header_mismatch"

        # ?csrf_token=… — the escape hatch for multipart uploads.
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        from_query = (query.get(CSRF_FIELD_NAME) or [""])[0]
        if from_query:
            ok = hmac.compare_digest(from_query, expected)
            return receive, ok, "ok" if ok else "query_mismatch"

        content_type = headers.get(b"content-type", b"").decode("latin-1").lower()
        if not content_type.startswith("application/x-www-form-urlencoded"):
            # multipart / JSON / anything else: header was the only option.
            return receive, False, "missing_token"

        # Only buffer when the client declared a size we are willing to hold.
        # Reading first and deciding later would mean that in *report* mode a
        # too-large form got forwarded with a truncated body — a security
        # control silently corrupting traffic is worse than the gap it closes.
        declared = headers.get(b"content-length", b"")
        try:
            length = int(declared) if declared else -1
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_BUFFER_BYTES:
            # Unknown (chunked) or oversized: the header/query token was the
            # only option and it was not supplied. The body is untouched.
            return receive, False, "body_unbuffered"

        body, replay = await self._buffer(receive)
        if body is None:
            return replay, False, "client_disconnected"
        try:
            fields = parse_qs(body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — an unparseable body fails closed
            return replay, False, "unparseable_body"
        from_form = (fields.get(CSRF_FIELD_NAME) or [""])[0]
        if not from_form:
            return replay, False, "missing_token"
        ok = hmac.compare_digest(from_form, expected)
        return replay, ok, "ok" if ok else "form_mismatch"

    @staticmethod
    async def _buffer(receive: Receive) -> tuple[bytes | None, Receive]:
        """Read the whole body and return a receive that replays it verbatim.

        The caller has already established, from ``Content-Length``, that the
        body is small enough to hold. The replay is byte-exact, so a route
        downstream sees precisely what the client sent — this middleware must
        never be able to alter a request it decided to let through.
        """
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # http.disconnect — hand it straight back, stop reading.
                # Bound as a default so the closure captures this exact
                # message, not whatever the loop variable ends up holding.
                async def _disconnected(_msg: Message = message) -> Message:
                    return _msg

                return None, _disconnected
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        sent = False

        async def _replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return body, _replay

    @staticmethod
    async def _reject(scope: Scope, send: Send, path: str) -> None:
        if path.startswith("/api/"):
            body = (
                b'{"ok":false,"error":"CSRF-token missing or invalid",'
                b'"detail":"csrf_failed"}'
            )
            content_type = b"application/json"
        else:
            body = (
                "<!doctype html><meta charset=utf-8>"
                "<body style='font-family:system-ui,sans-serif;background:#0b0b0f;"
                "color:#eee;padding:3rem;text-align:center'>"
                "<h2>Запрос отклонён</h2><p>Проверка безопасности не прошла. "
                "Обнови страницу и повтори действие.</p></body>"
            ).encode("utf-8")
            content_type = b"text/html; charset=utf-8"
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _wrap_send(
        self,
        scope: Scope,
        headers: dict[bytes, bytes],
        send: Send,
        expected: str,
        cookies: dict[str, str],
    ) -> Send:
        """Publish the CSRF cookie when it is missing or out of date.

        Only on **HTML** responses. Every page load fires a dozen parallel
        ``/static/*`` requests; attaching a ``Set-Cookie`` to each of them
        would put the same header on every asset for no benefit. Persona is
        server-rendered — every entry point into the app is an HTML document,
        so the cookie is always in place before any script runs.
        """
        if not expected or cookies.get(CSRF_COOKIE_NAME) == expected:
            return send
        secure = _is_https(headers, scope.get("scheme", "http"))
        cookie_value = _cookie_header(expected, secure).encode("latin-1")

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                is_html = any(
                    name.lower() == b"content-type"
                    and b"text/html" in value.lower()
                    for name, value in response_headers
                )
                if is_html:
                    message = dict(message)
                    message["headers"] = [
                        *response_headers,
                        (b"set-cookie", cookie_value),
                    ]
            await send(message)

        return _send
