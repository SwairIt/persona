"""One-shot setup wizard — first-run onboarding (v0.50).

The wizard surfaces every choice a brand-new install actually needs in a
single tall form: appearance, capture cadence, OCR languages, optional
BYO LLM credentials and the three-tier retention windows. Every field is
pre-filled from whatever is already on disk (env / .env via
``get_settings()`` and kv_settings overrides) so an existing user who
re-visits ``/setup`` sees their current configuration rather than the
factory defaults.

A successful POST writes each value to ``kv_settings`` (the same backing
store the per-feature settings pages use, so the override system keeps
working without surprise) and finally flips ``setup_complete=true``.
That flag is what :mod:`app.web.routes.setup_gate` keys off to redirect
brand-new installs to the wizard before they hit the timeline.

If the user supplies a BYO LLM key:
    * Provider must be one of the supported names (anthropic / openai /
      groq) — anything else is rejected with a 400 so we never persist a
      typo that would silently break /ask later.
    * The key is stashed in the encrypted vault (v0.33) under
      ``llm_api_key`` when ``cryptography`` is installed *and* the user
      ticked the master-password checkbox; otherwise it falls back to a
      plain ``kv_settings`` row (``byo_api_key``) so we degrade
      gracefully on the import-missing path. The provider name is always
      a kv row (``byo_api_provider``) because it is not secret.
    * An empty key is treated as "leave alone" — re-visiting the wizard
      after configuring once should not wipe credentials on save.

The wizard is *idempotent*: re-visiting after ``setup_complete=true`` is
a valid action, the template renders an "already configured" banner,
and POSTing again rewrites the same kv rows without flipping the flag a
second time (it is already true). This matters because Persona is
single-user but desktop browsers happily double-submit forms.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.ocr.languages import (
    get_configured_languages,
    get_installed_languages,
    set_configured_languages,
)
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.vault import set_secret
from app.web.templates_engine import invalidate_theme_cache, templates

router = APIRouter(tags=["setup"])
log = get_logger("persona.setup")


# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

_VALID_THEMES: Final[frozenset[str]] = frozenset({"dark", "light", "auto"})
_DEFAULT_THEME: Final[str] = "dark"

_VALID_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"anthropic", "openai", "groq", "gemini"}
)

# Match the validators on ``Settings`` so the wizard never persists a
# value the running config would reject on the next process boot.
_MIN_CAPTURE_SECONDS: Final[float] = 0.5
_MAX_CAPTURE_SECONDS: Final[float] = 60.0
_DEFAULT_CAPTURE_SECONDS: Final[float] = 5.0

_MIN_RETENTION_DAYS: Final[int] = 1
_MAX_RETENTION_DAYS: Final[int] = 3650

_DEFAULT_WARM_DAYS: Final[int] = 7
_DEFAULT_COLD_DAYS: Final[int] = 30
_DEFAULT_DELETE_DAYS: Final[int] = 180

# Key names used inside kv_settings. Centralised here so the gate
# module and any future export hooks can re-import without grepping for
# string literals.
KV_SETUP_COMPLETE: Final[str] = "setup_complete"
KV_THEME: Final[str] = "theme"
KV_CAPTURE_INTERVAL: Final[str] = "capture_interval_seconds"
KV_BYO_API_PROVIDER: Final[str] = "byo_api_provider"
KV_BYO_API_KEY: Final[str] = "byo_api_key"
KV_WARM_DAYS: Final[str] = "tier_warm_after_days"
KV_COLD_DAYS: Final[str] = "tier_cold_after_days"
KV_DELETE_DAYS: Final[str] = "retention_days"

# Vault row name for the BYO LLM key when ``cryptography`` is available.
VAULT_LLM_API_KEY: Final[str] = "llm_api_key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def is_setup_complete() -> bool:
    """Return True iff the wizard has been completed at least once.

    Exposed so :mod:`app.web.routes.setup_gate` can ask without
    re-implementing the kv lookup. Any value other than the literal
    string ``"true"`` is treated as "not done" — that includes ``None``,
    legacy boolean serialisations like ``"True"`` (we always write
    lowercase) and obvious typos.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, KV_SETUP_COMPLETE)
    return raw == "true"


async def _load_current() -> dict[str, str]:
    """Snapshot the values the wizard pre-fills on render.

    Reads kv overrides first and falls back to the ``Settings``
    defaults, so the form mirrors whatever the *running* process would
    use on the next boot.
    """
    cfg = get_settings()
    async with get_connection() as conn:
        kv_theme = await get_kv(conn, KV_THEME)
        kv_interval = await get_kv(conn, KV_CAPTURE_INTERVAL)
        kv_provider = await get_kv(conn, KV_BYO_API_PROVIDER)
        kv_warm = await get_kv(conn, KV_WARM_DAYS)
        kv_cold = await get_kv(conn, KV_COLD_DAYS)
        kv_delete = await get_kv(conn, KV_DELETE_DAYS)

    theme = kv_theme if kv_theme in _VALID_THEMES else cfg.theme
    if theme not in _VALID_THEMES:
        theme = _DEFAULT_THEME

    return {
        "theme": theme,
        "capture_interval_seconds": kv_interval or str(cfg.capture_interval_seconds),
        "llm_provider": (kv_provider or cfg.byo_api_provider or "").lower(),
        "retention_warm_days": kv_warm or str(cfg.tier_warm_after_days),
        "retention_cold_days": kv_cold or str(cfg.tier_cold_after_days),
        "retention_delete_days": kv_delete or str(cfg.retention_days),
    }


def _parse_float(value: str, *, lo: float, hi: float, default: float, field: str) -> float:
    """Parse ``value`` as a float clamped to ``[lo, hi]``.

    Empty strings fall through to ``default`` — convenient for partial
    wizard re-submits where the user only changed a couple of fields.
    Out-of-range values 400 rather than silently clamping so the user
    actually sees what they typed get rejected.
    """
    stripped = value.strip()
    if not stripped:
        return default
    try:
        parsed = float(stripped)
    except ValueError as exc:
        msg = f"{field} must be a number, got '{value}'"
        raise HTTPException(status_code=400, detail=msg) from exc
    if parsed < lo or parsed > hi:
        msg = f"{field} must be between {lo} and {hi}, got {parsed}"
        raise HTTPException(status_code=400, detail=msg)
    return parsed


def _parse_int(value: str, *, lo: int, hi: int, default: int, field: str) -> int:
    """Parse ``value`` as an int clamped to ``[lo, hi]``."""
    stripped = value.strip()
    if not stripped:
        return default
    try:
        parsed = int(stripped)
    except ValueError as exc:
        msg = f"{field} must be an integer, got '{value}'"
        raise HTTPException(status_code=400, detail=msg) from exc
    if parsed < lo or parsed > hi:
        msg = f"{field} must be between {lo} and {hi}, got {parsed}"
        raise HTTPException(status_code=400, detail=msg)
    return parsed


def _validate_retention_ordering(warm: int, cold: int, delete: int) -> None:
    """Reject windows that would let the worker run in the wrong order.

    The retention worker assumes ``warm < cold < delete``. The kv overrides
    do not enforce that on their own, so the wizard does it here — any
    other ordering would either re-promote a frame from cold to warm or
    delete it before it ever leaves the hot tier.
    """
    if warm >= cold:
        msg = (
            f"Warm window ({warm} days) must be strictly less than "
            f"cold window ({cold} days)."
        )
        raise HTTPException(status_code=400, detail=msg)
    if cold >= delete:
        msg = (
            f"Cold window ({cold} days) must be strictly less than "
            f"delete window ({delete} days)."
        )
        raise HTTPException(status_code=400, detail=msg)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
async def setup_wizard_page(request: Request) -> HTMLResponse:
    """Render the wizard with current values pre-filled.

    Renders the same template whether or not the wizard has been
    completed; the only difference is the ``already_configured`` banner
    on the second visit. That keeps deep-linking to ``/setup`` from a
    bookmark a sensible action.
    """
    current = await _load_current()
    installed_languages = await get_installed_languages()
    configured_languages = await get_configured_languages()
    already = await is_setup_complete()

    return templates.TemplateResponse(
        request,
        "setup_wizard.html",
        {
            "title": "Welcome to Persona",
            "active_nav": "settings",
            "current": current,
            "installed_languages": installed_languages,
            "configured_languages_set": set(configured_languages),
            "providers": sorted(_VALID_PROVIDERS),
            "theme_options": (
                ("dark", "Dark", "Low-light palette, easy on the eyes after sunset."),
                ("light", "Light", "High-contrast palette for bright rooms."),
                ("auto", "Auto", "Follow the operating system's preference."),
            ),
            "already_configured": already,
        },
    )


@router.post("/setup")
async def setup_wizard_save(request: Request) -> RedirectResponse:
    """Persist every wizard field in one transaction, then go to /timeline.

    The handler reads the form manually so the repeated
    ``ocr_languages`` checkbox group can be collected as a list —
    FastAPI's ``Form(...)`` only returns the first value of a repeated
    field, which would silently drop every language past the first.
    """
    form = await request.form()

    # ---- Theme ----------------------------------------------------------
    theme_raw = str(form.get("theme", _DEFAULT_THEME)).strip().lower()
    if theme_raw not in _VALID_THEMES:
        log.warning("setup.save.bad_theme", value=theme_raw)
        raise HTTPException(status_code=400, detail=f"Unknown theme '{theme_raw}'")

    # ---- Capture cadence -----------------------------------------------
    capture_interval = _parse_float(
        str(form.get("capture_interval_seconds", "")),
        lo=_MIN_CAPTURE_SECONDS,
        hi=_MAX_CAPTURE_SECONDS,
        default=_DEFAULT_CAPTURE_SECONDS,
        field="capture_interval_seconds",
    )

    # ---- OCR languages --------------------------------------------------
    raw_langs = [str(v) for v in form.getlist("ocr_languages")]
    # An empty selection during onboarding is *not* an error — the user
    # may not have OCR installed yet. We only call into the validator
    # when at least one box is ticked so we don't reject the whole form
    # on a fresh install with no language packs detected.
    if raw_langs:
        try:
            await set_configured_languages(raw_langs)
        except ValueError as exc:
            log.warning("setup.save.bad_ocr_langs", langs=raw_langs, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- BYO LLM credentials -------------------------------------------
    llm_provider = str(form.get("llm_provider", "")).strip().lower()
    llm_api_key = str(form.get("llm_api_key", "")).strip()
    llm_master_password = str(form.get("llm_master_password", "")).strip()

    if llm_provider and llm_provider not in _VALID_PROVIDERS:
        log.warning("setup.save.bad_provider", provider=llm_provider)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown LLM provider '{llm_provider}'",
        )

    # Retention -----------------------------------------------------------
    warm_days = _parse_int(
        str(form.get("retention_warm_days", "")),
        lo=_MIN_RETENTION_DAYS,
        hi=_MAX_RETENTION_DAYS,
        default=_DEFAULT_WARM_DAYS,
        field="retention_warm_days",
    )
    cold_days = _parse_int(
        str(form.get("retention_cold_days", "")),
        lo=_MIN_RETENTION_DAYS,
        hi=_MAX_RETENTION_DAYS,
        default=_DEFAULT_COLD_DAYS,
        field="retention_cold_days",
    )
    delete_days = _parse_int(
        str(form.get("retention_delete_days", "")),
        lo=_MIN_RETENTION_DAYS,
        hi=_MAX_RETENTION_DAYS,
        default=_DEFAULT_DELETE_DAYS,
        field="retention_delete_days",
    )
    _validate_retention_ordering(warm_days, cold_days, delete_days)

    # ---- Persist everything as kv rows ---------------------------------
    # We keep this inside a single connection so the writes either all
    # land or all fail together — important when the user refreshes
    # mid-save and we don't want to leave a half-configured install.
    vault_status: str | None = None

    async with get_connection() as conn:
        await set_kv(conn, KV_THEME, theme_raw)
        await set_kv(conn, KV_CAPTURE_INTERVAL, f"{capture_interval:g}")
        await set_kv(conn, KV_WARM_DAYS, str(warm_days))
        await set_kv(conn, KV_COLD_DAYS, str(cold_days))
        await set_kv(conn, KV_DELETE_DAYS, str(delete_days))

        # Provider name is not secret — always a kv row.
        if llm_provider:
            await set_kv(conn, KV_BYO_API_PROVIDER, llm_provider)

        # Key: empty means "leave whatever is on file alone". A non-empty
        # value either goes into the vault (if cryptography + a master
        # password are both available) or a plain kv row as a fallback.
        if llm_api_key:
            if llm_master_password:
                result = await set_secret(
                    VAULT_LLM_API_KEY,
                    llm_api_key,
                    llm_master_password,
                )
                vault_status = str(result.get("status"))
                if vault_status != "ok":
                    # Vault unavailable (missing cryptography) — fall back
                    # to plain kv. The audit trail in the structured log
                    # records *why* we degraded.
                    await set_kv(conn, KV_BYO_API_KEY, llm_api_key)
            else:
                await set_kv(conn, KV_BYO_API_KEY, llm_api_key)

        await set_kv(conn, KV_SETUP_COMPLETE, "true")

    # The theme cache lives at the Jinja layer; drop it so the redirect
    # render reflects the just-saved value rather than the previous one.
    invalidate_theme_cache()

    log.info(
        "setup.save.ok",
        theme=theme_raw,
        capture_interval_seconds=capture_interval,
        ocr_languages=raw_langs,
        llm_provider=llm_provider or "(none)",
        llm_key_provided=bool(llm_api_key),
        llm_key_in_vault=vault_status == "ok",
        retention_warm_days=warm_days,
        retention_cold_days=cold_days,
        retention_delete_days=delete_days,
    )
    return RedirectResponse(url="/timeline", status_code=303)
