"""Секреты платёжных провайдеров + переключатели биллинга.

Креды НЕ хранятся в БД и НЕ в git: сначала из переменных окружения, иначе из
файла ``{PERSONA_DATA_DIR}/billing_secrets.json`` (рядом с БД, вне репо, chmod
600). Это сознательно — прошлый аудит ловил plaintext-секреты в БД/HTML.

Два провайдера:
  * ``yookassa``  — исторический скелет (API v3, вебхук без подписи);
  * ``robokassa`` — подписанные ссылка/ResultURL (см. :mod:`app.billing.robokassa`).

Два независимых kv-переключателя (оба «выкл» по умолчанию, читаются из БД):
  * ``billing_enabled``  — вообще показывать ли платные тарифы и принимать оплату;
  * ``payment_provider`` — какой провайдер обслуживает оплату (``none`` по деф.).

ВАЖНО: ни один пароль не должен попасть в лог/шаблон/ответ. Датаклассы кредов
объявлены с ``repr=False`` на секретных полях — случайный ``log.info(creds)``
напечатает логин и режим, но не пароли.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- общие утилиты

PROVIDERS: tuple[str, ...] = ("none", "robokassa", "yookassa")
_DEFAULT_PROVIDER = "none"

# kv-кэш: /pricing публичная и горячая, лишний SELECT на каждый хит не нужен.
_KV_TTL = 30.0
_kv_cache: dict[str, tuple[float, str]] = {}


def data_dir() -> Path:
    d = os.environ.get("PERSONA_DATA_DIR")
    return Path(d) if d else Path.home() / ".persona"


def _secrets_file() -> Path:
    return data_dir() / "billing_secrets.json"


def _read_secrets() -> dict:
    f = _secrets_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_secrets(data: dict) -> None:
    f = _secrets_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    try:
        os.chmod(f, 0o600)  # best-effort (на Windows игнорируется)
    except OSError:
        pass


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def reset_cache() -> None:
    """Сбросить кэш kv-флагов (тесты / сразу после смены настройки владельцем)."""
    _kv_cache.clear()


async def _kv(key: str, default: str) -> str:
    """Прочитать kv-флаг с коротким TTL-кэшем. Любой сбой БД → ``default``."""
    now = time.monotonic()
    hit = _kv_cache.get(key)
    if hit is not None and now - hit[0] < _KV_TTL:
        return hit[1]
    value = default
    try:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv  # noqa: PLC0415

        async with get_connection() as conn:
            raw = await get_kv(conn, key)
        if raw is not None and str(raw).strip():
            value = str(raw).strip()
    except Exception:  # noqa: BLE001 — недоступная БД не должна включать продажи
        value = default
    _kv_cache[key] = (now, value)
    return value


async def billing_enabled() -> bool:
    """Продаём ли мы сейчас (kv ``billing_enabled``). ВЫКЛ по умолчанию.

    Пока «0» — витрина и кабинет выглядят ровно как сегодня: карточки Pro нет,
    /billing/checkout отвечает отказом, ResultURL ничего не активирует. Владелец
    переводит флаг в «1» РУКАМИ, когда закрыты юридические вопросы (оферта, ИП/
    самозанятость, чеки 54-ФЗ) и заведён магазин в Robokassa.
    """
    return await _kv("billing_enabled", "0") == "1"


async def active_provider() -> str:
    """kv ``payment_provider`` ∈ none|robokassa|yookassa. По умолчанию ``none``."""
    value = (await _kv("payment_provider", _DEFAULT_PROVIDER)).lower()
    return value if value in PROVIDERS else _DEFAULT_PROVIDER


# ---------------------------------------------------------------- ЮKassa

@dataclass(frozen=True)
class YooKassaCredentials:
    shop_id: str
    secret_key: str = field(repr=False)
    live: bool


def get_credentials() -> YooKassaCredentials | None:
    """shopId+secret_key ЮKassa: env > файл в data_dir. None — если не настроено."""
    shop = _env("PERSONA_YOOKASSA_SHOP_ID")
    secret = _env("PERSONA_YOOKASSA_SECRET_KEY")
    if shop and secret:
        live = (os.environ.get("PERSONA_YOOKASSA_LIVE") or "1").strip() == "1"
        return YooKassaCredentials(shop, secret, live)
    data = _read_secrets()
    shop = str(data.get("shop_id") or "").strip()
    secret = str(data.get("secret_key") or "").strip()
    if shop and secret:
        return YooKassaCredentials(shop, secret, bool(data.get("live", True)))
    return None


def is_configured() -> bool:
    """Настроена ли ЮKassa. Историческое имя — на него опираются воркер и тесты."""
    return get_credentials() is not None


def save_credentials(shop_id: str, secret_key: str, live: bool = True) -> None:
    """Owner-only: записать креды ЮKassa в data-dir файл (не в git, не в БД)."""
    data = _read_secrets()
    data.update({"shop_id": shop_id.strip(), "secret_key": secret_key.strip(), "live": live})
    _write_secrets(data)


# ---------------------------------------------------------------- Robokassa

# Алгоритмы, которые Robokassa разрешает выбрать в технастройках магазина.
# По умолчанию у них MD5; мы по умолчанию просим SHA256 — он тоже поддержан
# везде, а MD5 для подписи с секретом сегодня выбирать незачем. ГЛАВНОЕ: в коде
# и в кабинете Robokassa алгоритм должен совпадать, иначе подпись не сойдётся.
ROBOKASSA_HASHES: tuple[str, ...] = ("md5", "sha1", "sha256", "sha384", "sha512", "ripemd160")
_DEFAULT_HASH = "sha256"

# Системы налогообложения и ставки НДС из документации по фискализации.
ROBOKASSA_SNO: tuple[str, ...] = ("osn", "usn_income", "usn_income_outcome", "esn", "patent")
_DEFAULT_SNO = "usn_income"
_DEFAULT_TAX = "none"


@dataclass(frozen=True)
class RobokassaCredentials:
    """Креды и режим магазина Robokassa.

    ``password1``/``password2`` УЖЕ разрешены под текущий режим: в тестовом
    режиме здесь лежат тестовые пароли (Robokassa требует именно их при
    ``IsTest=1``, иначе отвечает ошибкой 29). Оба поля ``repr=False``.
    """

    login: str
    password1: str = field(repr=False)
    password2: str = field(repr=False)
    is_test: bool = False
    hash_algo: str = _DEFAULT_HASH
    receipt_enabled: bool = False
    sno: str = _DEFAULT_SNO
    tax: str = _DEFAULT_TAX
    inv_offset: int = 0


def _normalise_hash(raw: str) -> str:
    value = (raw or "").strip().lower().replace("-", "")
    return value if value in ROBOKASSA_HASHES else _DEFAULT_HASH


def _normalise_sno(raw: str) -> str:
    value = (raw or "").strip().lower()
    return value if value in ROBOKASSA_SNO else _DEFAULT_SNO


def _as_bool(raw: str | bool | None, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    value = (raw or "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def get_robokassa_credentials() -> RobokassaCredentials | None:
    """Креды Robokassa: env > ``billing_secrets.json`` (секция ``robokassa``).

    Env-имена::

        PERSONA_ROBOKASSA_LOGIN
        PERSONA_ROBOKASSA_PASSWORD1        # боевой Пароль #1
        PERSONA_ROBOKASSA_PASSWORD2        # боевой Пароль #2
        PERSONA_ROBOKASSA_TEST_PASSWORD1   # тестовый Пароль #1
        PERSONA_ROBOKASSA_TEST_PASSWORD2   # тестовый Пароль #2
        PERSONA_ROBOKASSA_IS_TEST          # 1 → тестовый режим (IsTest=1)
        PERSONA_ROBOKASSA_HASH             # md5|sha1|sha256|sha384|sha512|ripemd160
        PERSONA_ROBOKASSA_RECEIPT_ENABLED  # 1 → слать Receipt (54-ФЗ)
        PERSONA_ROBOKASSA_SNO              # osn|usn_income|usn_income_outcome|esn|patent
        PERSONA_ROBOKASSA_TAX              # none|vat0|vat5|vat7|vat10|vat20|…
        PERSONA_ROBOKASSA_INV_OFFSET       # сдвиг нумерации InvId (см. ниже)

    ``INV_OFFSET`` нужен, если в магазине Robokassa уже были операции с такими
    же номерами (напр. после тестов): InvId должен быть уникален в пределах
    магазина, а мы выводим его из ``payment.id``, который стартует с 1.

    None — если логина/пары паролей под текущий режим нет.
    """
    login = _env("PERSONA_ROBOKASSA_LOGIN")
    section: dict = {}
    if not login:
        section = _read_secrets().get("robokassa") or {}
        if not isinstance(section, dict):
            section = {}
        login = str(section.get("login") or "").strip()
    if not login:
        return None

    def pick(env_name: str, file_key: str) -> str:
        return _env(env_name) or str(section.get(file_key) or "").strip()

    is_test = _as_bool(
        _env("PERSONA_ROBOKASSA_IS_TEST") or section.get("is_test"), default=False
    )
    if is_test:
        p1 = pick("PERSONA_ROBOKASSA_TEST_PASSWORD1", "test_password1")
        p2 = pick("PERSONA_ROBOKASSA_TEST_PASSWORD2", "test_password2")
    else:
        p1 = pick("PERSONA_ROBOKASSA_PASSWORD1", "password1")
        p2 = pick("PERSONA_ROBOKASSA_PASSWORD2", "password2")
    if not p1 or not p2:
        return None

    try:
        offset = int(_env("PERSONA_ROBOKASSA_INV_OFFSET") or section.get("inv_offset") or 0)
    except (TypeError, ValueError):
        offset = 0

    return RobokassaCredentials(
        login=login,
        password1=p1,
        password2=p2,
        is_test=is_test,
        hash_algo=_normalise_hash(_env("PERSONA_ROBOKASSA_HASH") or str(section.get("hash") or "")),
        receipt_enabled=_as_bool(
            _env("PERSONA_ROBOKASSA_RECEIPT_ENABLED") or section.get("receipt_enabled"),
            default=False,
        ),
        sno=_normalise_sno(_env("PERSONA_ROBOKASSA_SNO") or str(section.get("sno") or "")),
        tax=(_env("PERSONA_ROBOKASSA_TAX") or str(section.get("tax") or "") or _DEFAULT_TAX).strip(),
        inv_offset=max(0, offset),
    )


def is_robokassa_configured() -> bool:
    return get_robokassa_credentials() is not None


def save_robokassa_credentials(
    *,
    login: str,
    password1: str = "",
    password2: str = "",
    test_password1: str = "",
    test_password2: str = "",
    is_test: bool = True,
    hash_algo: str = _DEFAULT_HASH,
    receipt_enabled: bool = False,
    sno: str = _DEFAULT_SNO,
    tax: str = _DEFAULT_TAX,
    inv_offset: int = 0,
) -> None:
    """Owner-only: записать креды Robokassa в data-dir файл (не в git, не в БД)."""
    data = _read_secrets()
    data["robokassa"] = {
        "login": login.strip(),
        "password1": password1.strip(),
        "password2": password2.strip(),
        "test_password1": test_password1.strip(),
        "test_password2": test_password2.strip(),
        "is_test": bool(is_test),
        "hash": _normalise_hash(hash_algo),
        "receipt_enabled": bool(receipt_enabled),
        "sno": _normalise_sno(sno),
        "tax": (tax or _DEFAULT_TAX).strip(),
        "inv_offset": max(0, int(inv_offset or 0)),
    }
    _write_secrets(data)


def is_provider_configured(provider: str) -> bool:
    """Настроен ли конкретный провайдер (без похода в БД)."""
    if provider == "robokassa":
        return is_robokassa_configured()
    if provider == "yookassa":
        return is_configured()
    return False


async def checkout_ready() -> tuple[bool, str]:
    """``(можно ли принимать оплату, активный провайдер)``.

    True требует ВСЕГО сразу: kv ``billing_enabled='1'``, выбранный провайдер и
    его настроенные креды. Любое «нет» → продажи не работают.
    """
    if not await billing_enabled():
        return False, "none"
    provider = await active_provider()
    return is_provider_configured(provider), provider
