"""Auth gate — redirects un-authenticated requests to /landing.

Activation rule
---------------
The gate is OFF when the ``users`` table is empty (so a brand-new local
install still works without forcing the owner through signup). It flips
ON the first time any user signs up. Existing single-user installs that
never run signup keep working without changes.

The state is cached in a module-level boolean so the middleware doesn't
hit the DB on every request; we re-check the flag every 60 s and on
process start. After the first signup the cache flips ON within a
minute even without explicit cache invalidation.

Public allow-list
-----------------
Paths starting with these prefixes are always accessible:
  * /landing, /auth/*, /help, /static/*
  * /healthz, /api/health.json (so load balancers stay green)
  * /api/sync/*, /api/devices/heartbeat (agent-facing, auth via header)
  * /favicon.ico

Everything else needs ``persona_session`` cookie.
"""

from __future__ import annotations

import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth import SESSION_COOKIE_NAME, verify_session
from app.auth.exclusive import read_owner_exclusive_mode
from app.auth.owner import is_owner, is_primary_owner
from app.billing.service import has_active_sub as _has_active_sub
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv

log = get_logger("persona.auth_gate")

# Cache window (seconds). After signup the gate activates within this.
_FLAG_TTL = 60.0

# Module-level cache of "is the gate active right now?"
_cache: dict[str, float | bool] = {"value": False, "checked_at": 0.0}

# Prefixes that bypass auth. Order matters only insofar as readability.
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/landing",
    # Public marketing surface — blog + SEO files must be crawlable and
    # readable without a session (huge-SEO content lives under /blog).
    "/blog",
    # BUILD_PLAN Фаза C — публичные маркетинговые страницы (без сессии, для SEO).
    "/features",
    "/compare",
    "/pricing",
    "/security",
    "/privacy-policy",
    "/terms",
    "/roadmap",
    "/changelog",
    "/sitemap.xml",
    "/robots.txt",
    "/auth/",
    "/help",
    "/static/",
    "/healthz",
    "/api/health.json",
    "/api/sync/",
    "/api/devices/heartbeat",
    # T29 (2026-06-08) — remote capture agents (Mac) upload here with a
    # Bearer / X-Agent-Token, never a cookie. These were silently 401'd by
    # the gate ever since the first signup activated it, so NO agent
    # screenshot/audio ever landed. The routes enforce their own bearer
    # auth via app.remote_agents.verify_agent_token.
    "/api/agent/",
    # Навык Алисы (Яндекс.Диалоги) зовёт вебхук из интернета без cookie;
    # защита — секрет в пути (kv alice_webhook_secret), проверяется в роуте.
    "/api/alice/",
    # T28/T29 — the code-write-target device polls (/sync, down) and pushes
    # (/push, up) with its X-Device-Token, no cookie session. The routes
    # enforce auth + check the device is the chosen target.
    "/api/workspace/",
    # T29 — the Mac agent polls the live mic-pause flag here (no cookie). It
    # was NOT allowlisted, so the agent got a login redirect instead of JSON
    # → the mute flag never reached the agent and the mic never stopped.
    "/api/audio/mic",
    # T16 (2026-06-07) — iOS Shortcut hits these with X-Device-Token,
    # not a cookie session. The route itself enforces auth.
    "/api/ingest/",
    # T18 — installer.sh fetch happens from user's Mac terminal where
    # there's no session cookie. Single-use ``t`` query token is the auth.
    "/api/install/",
    # T31 ФАЗА F — голосовой ассистент: Mac-агент шлёт распознанную фразу,
    # опрашивает очередь озвучки и подтверждает её с Bearer/X-Agent-Token
    # (без cookie). Эндпоинты сами проверяют токен агента.
    "/api/voice/",
    # Биллинг: вебхук ЮKassa приходит из интернета без cookie (подлинность —
    # re-GET платежа через наш secret), а валидацию лицензии дёргает чужой
    # self-host тоже без сессии (rate-limit по IP в роуте).
    "/billing/webhook",
    "/api/v1/license",
    # LLM-воркер: ПК-агент дозванивается без cookie, авторизуется X-Worker-Token
    # (валидация в самом роуте). rotate-token/status — owner-only через свои
    # зависимости роута, поэтому открытие префикса в гейте безопасно.
    "/api/llm/worker",
    "/favicon.ico",
)


async def _gate_active() -> bool:
    """Return whether the gate should redirect un-authenticated requests."""
    now = time.monotonic()
    if now - float(_cache["checked_at"]) < _FLAG_TTL:
        return bool(_cache["value"])
    try:
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT 1 FROM users LIMIT 1")
            row = await cursor.fetchone()
        active = row is not None
    except Exception as exc:
        # DB hiccup — fail-open (gate inactive) so a transient SQLite
        # lock never bricks the whole site.
        log.warning("auth_gate.check_failed", error=str(exc))
        active = False
    _cache["value"] = active
    _cache["checked_at"] = now
    return active


def _is_public_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


# Пути, доступные НЕ-владельцу с активной подпиской (Pro/триал): только сам
# ИИ-ассистент (чат) и онбординг. Чат и память изолированы по user_id — он видит
# лишь СВОЁ. Захват/таймлайн/общие настройки владельца остаются закрытыми.
_PRO_PREFIXES: tuple[str, ...] = ("/chat", "/api/chat", "/onboarding")


def _is_pro_path(path: str) -> bool:
    # Защита от обхода нормализацией: если в пути есть ".." / "/../" — это попытка
    # вырваться из pro-префикса (напр. /chat/../now). НЕ считаем pro-путём, чтобы
    # такой путь не получил доступ по подписке. Легитимные /chat, /api/chat,
    # /onboarding точек-сегментов не содержат и не затрагиваются.
    if ".." in path:
        return False
    return any(path == p or path.startswith(p + "/") for p in _PRO_PREFIXES)


# ── Роле-основанная маршрутизация (F6-12) — ЗА ФИЧА-ФЛАГОМ, DEFAULT OFF ─────────
#
# Включается ТОЛЬКО когда kv ``role_gate_enabled == '1'``. По умолчанию (флаг
# отсутствует/любое другое значение) этот блок НЕ исполняется: гейт работает
# ровно как раньше (owner-based). Деплой без правки kv ничего не меняет.
#
# При ВКЛ флаге роль определяет, какие зоны видны:
#   * owner  — всё (суперсет; см. owner-gate ниже, он остаётся fallback);
#   * admin  — приложение + /admin/*, но НЕ /root (это зона владельца);
#   * member — приложение, но НЕ /admin/* и НЕ /root;
#   * viewer — только безопасные (GET/HEAD/OPTIONS) запросы.
# owner-gate (приватные данные) остаётся последним рубежом: даже при ВКЛ флаге
# не-владелец без подписки не получает приватную поверхность владельца.

# Кэш флага role_gate (как _FLAG_TTL у активности гейта). 60с.
_role_gate_cache: dict[str, float | bool] = {"value": False, "checked_at": 0.0}
_owner_exclusive_cache: dict[str, float | bool] = {
    "value": False,
    "checked_at": 0.0,
}

# Префиксы зон по ролям.
_ADMIN_PREFIXES: tuple[str, ...] = ("/admin",)
_OWNER_ONLY_PREFIXES: tuple[str, ...] = ("/root",)
# Методы, разрешённые viewer (read-only). Всё остальное (POST/PUT/...) — 403.
_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


async def _role_gate_enabled() -> bool:
    """True, если kv ``role_gate_enabled == '1'``. Fail-safe → False (OFF).

    Кэшируется на 60с, чтобы не бить в БД на каждый запрос. Любой сбой БД/чтения
    трактуется как «флаг ВЫКЛ» — то есть поведение остаётся текущим (owner-based),
    а не блокирующим. Это держит инвариант: ошибка резолва не отнимает доступ.
    """
    now = time.monotonic()
    if now - float(_role_gate_cache["checked_at"]) < _FLAG_TTL:
        return bool(_role_gate_cache["value"])
    enabled = False
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, "role_gate_enabled")
        enabled = str(raw).strip() == "1"
    except Exception as exc:  # noqa: BLE001 — сбой → OFF (старое поведение)
        log.debug("auth_gate.role_flag_failed", error=str(exc))
        enabled = False
    _role_gate_cache["value"] = enabled
    _role_gate_cache["checked_at"] = now
    return enabled


async def _owner_exclusive_enabled() -> bool:
    """Return whether private routes are restricted to the primary owner.

    The hosted instance can enable this with KV ``owner_exclusive_mode=1``
    without changing the behaviour of independent self-hosted installs.
    Once enabled, a transient database error keeps the last safe decision.
    """
    now = time.monotonic()
    if now - float(_owner_exclusive_cache["checked_at"]) < _FLAG_TTL:
        return bool(_owner_exclusive_cache["value"])
    enabled = bool(_owner_exclusive_cache["value"])
    try:
        enabled = await read_owner_exclusive_mode()
    except Exception as exc:  # noqa: BLE001 - private deployment fails closed
        log.warning("auth_gate.owner_exclusive_flag_failed", error=str(exc))
        enabled = True
    _owner_exclusive_cache["value"] = enabled
    _owner_exclusive_cache["checked_at"] = now
    return enabled


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """Точное совпадение зоны или вход в неё (``/admin`` либо ``/admin/...``)."""
    if ".." in path:
        # Нормализационный обход (/admin/../root): не считаем «своей» зоной —
        # пусть провалится в owner-gate fallback ниже (безопаснее).
        return False
    return any(path == p or path.startswith(p + "/") for p in prefixes)


async def _role_route_allows(uid: int | None, path: str, method: str) -> bool | None:
    """Решение роле-гейта для аутентифицированного НЕ-владельца.

    Возвращает:
      * ``True``  — роль явно разрешает этот путь (пропустить);
      * ``None``  — роле-гейт не выносит вердикт (отдать решение owner-gate
        fallback ниже: приватные данные владельца остаются защищены);
      * (``False`` сейчас не используется — отказ выражаем через ``None`` +
        fallback, чтобы НИКОГДА не отнять доступ, который даёт старый путь).

    Fail-safe: при сбое резолва роли → ``None`` (не блокируем, отдаём в fallback).
    """
    # Импорт локальный: избегаем циклов на старте и держим owner-gate автономным.
    from app.auth.guards import resolve_role

    try:
        role = await resolve_role(uid)
    except Exception as exc:  # noqa: BLE001 — сбой роли → отдать в fallback
        log.debug("auth_gate.role_resolve_failed", error=str(exc))
        return None

    # /root — только владелец. Не-владелец сюда не попадает по роли (owner идёт
    # отдельной веткой выше). admin/member/viewer — мимо, отдаём в fallback.
    if _matches(path, _OWNER_ONLY_PREFIXES):
        return None

    # viewer — только безопасные методы. Небезопасный метод → нет вердикта
    # (fallback решит; для приватных данных это всё равно отказ).
    if role == "viewer":
        return True if method in _SAFE_METHODS else None

    # admin — приложение + /admin/*. Разрешаем admin-зону явно.
    if role == "admin":
        return True

    # member — приложение, но НЕ /admin/* и НЕ /root. В admin-зону — нет вердикта
    # (отдаём fallback → отказ). В остальное приложение — пропускаем.
    if role == "member":
        return None if _matches(path, _ADMIN_PREFIXES) else True

    # Неизвестная роль → нет вердикта (fallback, безопасный путь).
    return None


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Redirect un-authenticated visitors to /landing once any user exists."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        path = request.url.path

        # Exact root "/" is public (landing for logged-out, redirect for
        # logged-in) — can't be a prefix in the allow-list since "/" prefixes
        # every path.
        if path == "/" or _is_public_path(path):
            return await call_next(request)

        # Exclusive deployments must never inherit the legacy bootstrap
        # fail-open path. Read the privacy flag first; lookup failure itself is
        # treated as exclusive so a transient DB error cannot expose private
        # routes.
        owner_exclusive = await _owner_exclusive_enabled()
        if not owner_exclusive and not await _gate_active():
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE_NAME)
        session = await verify_session(token) if token else None
        if session is not None:
            uid = session.get("user_id")
            if owner_exclusive:
                if await is_primary_owner(uid):
                    return await call_next(request)
                if path == "/pending":
                    return await call_next(request)
                if path.startswith("/api/"):
                    return Response(
                        content='{"detail":"owner access required"}',
                        status_code=403,
                        media_type="application/json",
                    )
                return RedirectResponse(url="/pending", status_code=303)
            if await is_owner(uid):
                # Владелец — суперсет: видит всё, всегда (и при ВКЛ роле-гейте).
                return await call_next(request)
            if path == "/pending" or path.startswith("/auth/"):
                return await call_next(request)
            if path == "/billing" or path.startswith("/billing/"):
                return await call_next(request)
            # Роле-основанная маршрутизация — ТОЛЬКО при kv role_gate_enabled=='1'.
            # При ВЫКЛ (дефолт) этот блок пропускается целиком → дальше идёт
            # СТАРЫЙ код-путь (owner-gate / pro), решения доступа не меняются.
            # При ВКЛ блок может лишь ДОПОЛНИТЕЛЬНО разрешить (admin→/admin и т.п.);
            # вердикт «нет» означает падение в тот же owner-gate fallback ниже,
            # поэтому никто не теряет доступ, который давал старый путь.
            if await _role_gate_enabled():
                verdict = await _role_route_allows(uid, path, request.method)
                if verdict is True:
                    return await call_next(request)
            # Не-владелец с активной подпиской → ТОЛЬКО ассистент (чат) + онбординг.
            # Чат/память изолированы по user_id (видит лишь своё). Личные данные
            # владельца (захват/таймлайн/общие настройки) — закрыты.
            if _is_pro_path(path) and await _has_active_sub(uid):
                return await call_next(request)
            if path.startswith("/api/"):
                return Response(
                    content='{"detail":"subscription required"}',
                    status_code=403,
                    media_type="application/json",
                )
            return RedirectResponse(url="/billing", status_code=303)

        # Browser nav → 303 to /landing. JSON / agent endpoints get 401
        # so they don't end up with HTML in their response body.
        if path.startswith("/api/"):
            return Response(
                content='{"detail":"authentication required"}',
                status_code=401,
                media_type="application/json",
            )
        return RedirectResponse(url="/landing", status_code=303)
