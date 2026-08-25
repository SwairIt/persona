"""Захват событий: буфер в памяти, псевдонимы, и решение про согласие.

═══════════════════════════════════════════════════════════════════════════
РЕШЕНИЕ ПРО СОГЛАСИЕ (152-ФЗ) — читать до правок
═══════════════════════════════════════════════════════════════════════════
На сайте уже есть баннер согласия и кука ``persona_consent`` (см.
``app/web/static/consent.js``), которая гейтит Яндекс.Метрику с вебвизором.
Первосторонняя аналитика — это тоже обработка, и она разложена на ДВА разных
по правовому основанию слоя. Разделение проведено по одному признаку: можно ли
по записи связать два визита одного человека.

1. **Обезличенный хит — БЕЗ согласия.** Строка «в 14:03 кто-то открыл
   ``/landing``, роль anonymous, класс устройства mobile». ``session_hash``
   = NULL, реферер не пишется, IP не пишется нигде и никогда. Связать два
   таких хита между собой невозможно by construction — это счётчик посещений,
   функционально идентичный строке в access-логе веб-сервера, который оператор
   и так обязан вести. Основание — законный интерес оператора обеспечивать
   работоспособность своего сервиса (то же, на котором в политике уже описаны
   журналы сервера). Без этого слоя верх воронки («сколько людей вообще
   увидело лендинг») не измеряется в принципе, а именно он владельцу и нужен.

2. **Связывание визитов (сессия, «первый визит», реферер, клики) — ТОЛЬКО
   с согласием** ``persona_consent=all``. Здесь появляется псевдоним, по
   которому два обращения склеиваются в одно посещение; это уже поведенческий
   профиль, и без явного согласия анонимного посетителя мы его не строим.

3. **Вошедший участник** пишется с ``user_id`` — это обработка в рамках
   договора (аккаунт, п. 5 ч. 1 ст. 6 152-ФЗ), плюс отдельный раздел политики,
   описывающий ровно этот сбор. Его след удаляется вместе с аккаунтом
   (``ON DELETE CASCADE`` в миграции 234) и попадает в его же выгрузку.

4. **Владелец инстанса** — это оператор, смотрящий на работу собственного
   сервера. Своё поведение он пишет всегда: согласия у самого себя не спрашивают.
   Роль пишется отдельной колонкой именно чтобы владельца можно было ВЫЧЕСТЬ
   из отчёта — иначе самый активный посетитель своего сайта это он сам.

Отключается всё целиком: kv ``analytics_enabled = 0`` (по умолчанию включено).

═══════════════════════════════════════════════════════════════════════════
ЧЕГО ЗДЕСЬ НЕТ
═══════════════════════════════════════════════════════════════════════════
* Сырой user-agent НЕ хранится — только класс ``desktop/mobile/bot/unknown``.
* IP НЕ хранится ни в каком виде. Он используется РОВНО один раз как вход
  односторонней HMAC-функции, чтобы посчитать псевдоним сессии анонима, и не
  попадает ни в одну колонку. Псевдоним пересолен сутками, поэтому склейка
  живёт до полуночи UTC и не переживает её.
* Полный реферер НЕ хранится — только хост. В query чужой страницы лежит
  поисковый запрос человека и чужие идентификаторы сессии.
* Значения полей форм НЕ собираются. ``label`` — это подпись, которую
  разработчик сам написал в ``data-track``, а не то, что человек ввёл.
* Записи сессии (вебвизора) здесь нет и не появится.

═══════════════════════════════════════════════════════════════════════════
ПРОИЗВОДИТЕЛЬНОСТЬ
═══════════════════════════════════════════════════════════════════════════
Инстанс уже упирается в диск и в SQLite-писателя (см. ``app/web/middleware/
throttle.py``), поэтому запись события на пути ответа ЗАПРЕЩЕНА. :func:`record`
— обычная синхронная функция: посчитать пару строк и положить dict в
``deque``. В БД пачку сливает фоновая задача раз в :data:`FLUSH_INTERVAL`
секунд одной транзакцией.

Если буфер переполнился (``BUFFER_LIMIT``) — старые события ТЕРЯЮТСЯ, а
счётчик потерь уезжает на дашборд. Осознанный выбор: аналитика не имеет права
ни блокировать запрос, ни съедать память сайта, и честная надпись «потеряно N
событий» лучше, чем и то и другое.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.logging_setup import get_logger

log = get_logger("persona.analytics.capture")

#: kv: главный рубильник инстанса. Отсутствует или ``1`` → включено.
KV_ENABLED = "analytics_enabled"
#: kv: соль для псевдонимов. Генерируется на инстансе, никуда не уезжает.
KV_SALT = "analytics_salt"

#: Кука решения по аналитике (ставится ``app/web/static/consent.js``).
CONSENT_COOKIE = "persona_consent"
CONSENT_GRANTED = "all"

FLUSH_INTERVAL = 5.0
BUFFER_LIMIT = 5_000
BATCH_LIMIT = 500
_STATE_TTL = 30.0

ROLE_ANONYMOUS = "anonymous"
ROLE_MEMBER = "member"
ROLE_OWNER = "owner"

KIND_VIEW = "view"
KIND_CLICK = "click"
KIND_SUBMIT = "submit"
KIND_OUTBOUND = "outbound"
VALID_KINDS = frozenset({KIND_VIEW, KIND_CLICK, KIND_SUBMIT, KIND_OUTBOUND})

#: Пути, которые в аналитику не попадают ВООБЩЕ. Статика и health-пробы —
#: это не «куда заходят люди», а фон; сама ручка приёма кликов не считает
#: собственные вызовы (иначе счётчик считал бы себя).
_SKIP_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/favicon",
    "/healthz",
    "/api/health.json",
    "/api/track",
    "/api/sync/",
    "/api/agent/",
    "/api/devices/",
    "/api/llm/worker",
    "/api/workspace/",
    "/api/voice/",
    "/api/ingest/",
    "/api/install/",
    "/api/alice/",
)

_BUFFER: deque[dict[str, Any]] = deque()
_dropped = 0
_flusher: asyncio.Task | None = None
_flusher_loop: asyncio.AbstractEventLoop | None = None

_state: dict[str, Any] = {"enabled": None, "salt": "", "checked_at": 0.0}


# ── конфигурация инстанса ─────────────────────────────────────────────────────


def reset_cache() -> None:
    """Сбросить кэш рубильника и соли (тесты; смена настройки владельцем)."""
    _state["enabled"] = None
    _state["salt"] = ""
    _state["checked_at"] = 0.0


async def refresh_state() -> dict[str, Any]:
    """Прочитать рубильник и соль, закэшировав на :data:`_STATE_TTL` секунд.

    Соль создаётся при первом обращении и живёт в kv — без неё псевдонимы были
    бы просто хешем IP, то есть обратимыми перебором адресного пространства.

    Fail-safe у этой функции РАЗВЁРНУТ в сторону «не писать»: если БД
    недоступна и прежнего ответа нет, аналитика молчит. Сломанная база — не
    повод начинать сбор, о котором мы не смогли спросить разрешения.
    """
    now = time.monotonic()
    if _state["enabled"] is not None and now - float(_state["checked_at"]) < _STATE_TTL:
        return _state
    try:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv, set_kv  # noqa: PLC0415

        async with get_connection() as conn:
            raw = await get_kv(conn, KV_ENABLED)
            salt = await get_kv(conn, KV_SALT)
            if not salt:
                # ``set_kv`` коммитит сам, поэтому НЕ заворачиваем его в
                # ``write_transaction``: явный BEGIN IMMEDIATE + внутренний
                # commit = «cannot commit - no transaction is active».
                salt = secrets.token_hex(32)
                await set_kv(conn, KV_SALT, salt)
        _state["enabled"] = str(raw).strip() != "0" if raw is not None else True
        _state["salt"] = salt
        _state["checked_at"] = now
    except Exception as exc:  # noqa: BLE001 — не смогли выяснить → не пишем
        log.debug("analytics.state_failed", error=str(exc))
        if _state["enabled"] is None:
            _state["enabled"] = False
    return _state


def is_enabled() -> bool:
    return bool(_state["enabled"])


def current_salt() -> str:
    """Соль псевдонимов, уже прочитанная :func:`refresh_state`. Пусто = ещё нет."""
    return str(_state.get("salt") or "")


# ── нормализация и обезличивание ──────────────────────────────────────────────

_ROUTE_TABLES: dict[int, list[tuple[Any, str]]] = {}


def normalise_path(app: Any, path: str) -> str:
    """Свернуть ``/chat/123`` к шаблону роута ``/chat/{session_id}``.

    Берётся ТАБЛИЦА РОУТОВ самого приложения, а не регулярка «замени цифры на
    {id}»: угадайка ломается ровно там, где параметр не число (``/day/2026-08-25``,
    ``/tag/работа``) — и владелец получил бы сто строк по одной вместо одной
    строки со ста хитами. Таблица кэшируется по ``id(app)``: она не меняется
    после старта.

    Незнакомый путь (404, чужой сканер) сворачивается в ``(unmatched)`` — иначе
    первый же бот с перебором URL раздул бы таблицу путей на десятки тысяч
    строк, которые ничего не говорят о том, «куда заходят».
    """
    table = _ROUTE_TABLES.get(id(app))
    if table is None:
        table = []
        for route in getattr(app, "routes", []) or []:
            regex = getattr(route, "path_regex", None)
            fmt = getattr(route, "path_format", None)
            if regex is not None and fmt:
                table.append((regex, str(fmt)))
        _ROUTE_TABLES[id(app)] = table
    for regex, fmt in table:
        try:
            if regex.match(path):
                return fmt
        except Exception:  # noqa: BLE001, S112 — битый роут не ломает запись
            continue
    return "(unmatched)"


def device_class(user_agent: str) -> str:
    """Грубый класс устройства. Сама строка UA НИКУДА не сохраняется.

    Три ответа вместо fingerprint: владельцу нужно знать «сайт открывают с
    телефона или с компьютера» и «это вообще человек или краулер», а версия
    браузера и модель телефона — это уже данные для узнавания человека.
    """
    ua = (user_agent or "").lower()
    if not ua:
        return "unknown"
    if any(
        marker in ua
        for marker in ("bot", "crawler", "spider", "slurp", "curl", "wget", "python-")
    ):
        return "bot"
    if any(marker in ua for marker in ("mobi", "android", "iphone", "ipad", "ipod")):
        return "mobile"
    return "desktop"


def referrer_host(referrer: str) -> str:
    """Только хост реферера, без схемы, пути и query.

    В query чужой страницы лежит поисковый запрос человека и чужие
    session id. Нам нужно «откуда пришли», а не «что человек искал».
    """
    ref = (referrer or "").strip()
    if not ref:
        return ""
    without_scheme = ref.split("://", 1)[-1]
    host = without_scheme.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.split("@")[-1].split(":")[0].lower()
    if len(host) > 100 or "." not in host:
        return ""
    return host


def _digest(salt: str, material: str) -> str:
    return hmac.new(
        salt.encode("utf-8"), material.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]


def session_pseudonym(
    *,
    salt: str,
    session_token: str | None,
    client_ip: str | None,
    device: str,
    day: str,
    consented: bool,
    authenticated: bool,
) -> str | None:
    """Псевдоним посещения — или ``None``, если связывать визиты нельзя.

    * вошедший → HMAC от токена сессии: умирает вместе с сессией, обратно не
      разворачивается, и ничего нового о человеке не сообщает (мы и так знаем
      его ``user_id``);
    * аноним С СОГЛАСИЕМ → HMAC от (сутки + IP + класс устройства): IP здесь
      только вход функции, в базу не попадает, а пересолка сутками ограничивает
      склейку одним днём;
    * аноним БЕЗ согласия → ``None``: его хиты остаются несвязываемыми.
    """
    if not salt:
        return None
    if authenticated and session_token:
        return _digest(salt, "s:" + session_token)
    if not consented:
        return None
    return _digest(salt, f"a:{day}:{client_ip or '-'}:{device}")


# ── буфер ─────────────────────────────────────────────────────────────────────


def should_skip(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


def record(
    *,
    kind: str,
    path: str,
    role: str,
    device: str = "unknown",
    label: str = "",
    referrer: str = "",
    session_hash: str | None = None,
    user_id: int | None = None,
    first_view: bool = False,
    status: int | None = None,
) -> bool:
    """Положить событие в буфер. НИКОГДА не бросает и никуда не ходит.

    Возвращает ``True``, если событие принято. Любая ошибка внутри проглочена:
    запрос, который она сопровождает, не имеет права упасть из-за счётчика.
    """
    try:
        if not is_enabled() or kind not in VALID_KINDS:
            return False
        now = datetime.now(UTC)
        if len(_BUFFER) >= BUFFER_LIMIT:
            global _dropped
            _BUFFER.popleft()
            _dropped += 1
        _BUFFER.append(
            {
                "occurred_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "day": now.strftime("%Y-%m-%d"),
                "kind": kind,
                "path": (path or "/")[:200],
                "label": (label or "")[:120],
                "role": role,
                "device": device or "unknown",
                "referrer_host": (referrer or "")[:100],
                "session_hash": session_hash,
                "user_id": user_id,
                "first_view": 1 if first_view else 0,
                "status": status,
            }
        )
        _ensure_flusher()
        return True
    except Exception as exc:  # noqa: BLE001 — счётчик не ломает продукт
        log.debug("analytics.record_failed", error=str(exc))
        return False


def buffered() -> int:
    return len(_BUFFER)


def dropped() -> int:
    """Сколько событий потеряно из-за переполнения буфера (честность отчёта)."""
    return _dropped


def reset_buffer() -> None:
    _BUFFER.clear()
    global _dropped
    _dropped = 0


def _ensure_flusher() -> None:
    global _flusher, _flusher_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _flusher is not None and not _flusher.done() and _flusher_loop is loop:
        return
    _flusher_loop = loop
    _flusher = loop.create_task(_flush_loop())


async def _flush_loop() -> None:
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL)
            await flush()
        except asyncio.CancelledError:
            await flush()
            raise
        except Exception as exc:  # noqa: BLE001 — флашер не умирает молча
            log.warning("analytics.flush_loop_failed", error=str(exc))


async def flush() -> int:
    """Слить буфер в БД одной пачкой. Ошибка записи ТЕРЯЕТ пачку, а не висит.

    Возврат событий в буфер при сбое выглядит заманчиво, но означает, что
    сломанный диск превращает аналитику в растущую утечку памяти, а каждый
    следующий флаш повторяет тот же отказ. Аналитика — расходуемые данные:
    лучше дырка в графике, чем деградация сайта.
    """
    if not _BUFFER:
        return 0
    batch: list[dict[str, Any]] = []
    while _BUFFER and len(batch) < BATCH_LIMIT:
        batch.append(_BUFFER.popleft())
    try:
        from app.analytics import store  # noqa: PLC0415

        return await store.insert_events(batch)
    except Exception as exc:  # noqa: BLE001
        log.warning("analytics.flush_failed", error=str(exc), lost=len(batch))
        return 0


__all__ = [
    "BUFFER_LIMIT",
    "CONSENT_COOKIE",
    "CONSENT_GRANTED",
    "KIND_CLICK",
    "KIND_OUTBOUND",
    "KIND_SUBMIT",
    "KIND_VIEW",
    "KV_ENABLED",
    "KV_SALT",
    "ROLE_ANONYMOUS",
    "ROLE_MEMBER",
    "ROLE_OWNER",
    "VALID_KINDS",
    "buffered",
    "current_salt",
    "device_class",
    "dropped",
    "flush",
    "is_enabled",
    "normalise_path",
    "record",
    "referrer_host",
    "refresh_state",
    "reset_buffer",
    "reset_cache",
    "session_pseudonym",
    "should_skip",
]
