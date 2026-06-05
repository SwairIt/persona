"""FastAPI dependency for read-only Bearer auth (v1.40).

This is the per-route counterpart to the v0.34
:class:`app.web.middleware.api_auth.ApiAuthMiddleware`. Where the v0.34
middleware gates the whole ``/api/*`` prefix on a setting, this module
exposes a fine-grained :func:`get_token_owner` dependency that an
individual route can declare to require a valid Bearer token *regardless*
of the global ``PERSONA_API_AUTH_REQUIRED`` flag — useful for new
read-only endpoints we want to ship to third parties without forcing all
existing ``/api/*`` consumers through auth.

The dependency:

1. Pulls the ``Authorization`` header.
2. Validates the ``Bearer <token>`` shape.
3. Resolves the token via :func:`app.api_tokens_admin.verify_token`.
4. Bumps ``last_used_at`` + ``use_count`` via :func:`record_token_use`.
5. Returns the token metadata for the handler to use.

Any failure raises :class:`HTTPException(401)` with a JSON body shaped
``{"detail": {"error": "unauthorized", "reason": ...}}``. The reason is
deliberately surface-level (``missing``/``malformed``/``unknown``/
``revoked``/``expired``) so a misconfigured client can self-diagnose
without leaking *which* row matched — there's no oracle for "this hash
exists but is revoked" vs "this hash doesn't exist".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Header, HTTPException

from app.api_tokens_admin import record_token_use, verify_token
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.api_tokens_admin import VerifyOk

log = get_logger("persona.api_tokens")

_BEARER_PREFIX = "Bearer "


def _unauthorized(reason: str) -> HTTPException:
    """Build the canonical 401 used by every rejection path.

    We attach ``WWW-Authenticate: Bearer`` so well-behaved clients (curl
    ``--digest``, browsers, OpenAPI generators) know to prompt for a
    token rather than retrying with the wrong scheme.
    """
    return HTTPException(
        status_code=401,
        detail={"error": "unauthorized", "reason": reason},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_token_owner(
    authorization: str | None = Header(default=None),
) -> VerifyOk:
    """FastAPI dependency: require a valid Bearer token, return its metadata.

    Usage::

        @router.get("/api/v1/screenshots.json")
        async def shots(owner: VerifyOk = Depends(get_token_owner)) -> ...:
            ...

    The returned :class:`VerifyOk` dict carries ``token_id``, ``scopes``
    and ``label`` — the handler may inspect ``scopes`` to enforce
    write/admin operations on top of the baseline "is this token live"
    check the dependency performs.

    Never logs the raw token. On rejection only the *reason* is logged.
    """
    if authorization is None or not authorization.strip():
        log.info("api_token.dep.reject", reason="missing")
        raise _unauthorized("missing")

    header = authorization.strip()
    if not header.startswith(_BEARER_PREFIX):
        log.info("api_token.dep.reject", reason="malformed")
        raise _unauthorized("malformed")

    raw = header[len(_BEARER_PREFIX) :].strip()
    if not raw:
        log.info("api_token.dep.reject", reason="malformed")
        raise _unauthorized("malformed")

    result = await verify_token(raw)
    if not result["valid"]:
        # ``verify_token`` already logged the specific reason; we just
        # forward it to the client. ``result.get(...)`` returns ``object``
        # under the union, so we coerce to ``str`` before handing it back.
        reason = str(result.get("reason", "invalid"))
        raise _unauthorized(reason)

    # mypy: narrow to VerifyOk now that ``valid`` is True.
    ok: VerifyOk = result  # type: ignore[assignment]
    await record_token_use(ok["token_id"])
    return ok
