"""Шифрование данных участников «в покое» (at rest).

ЧТО ЭТО ЗАКРЫВАЕТ И ЧТО НЕ ЗАКРЫВАЕТ — ЧИТАТЬ ДО ПРАВОК
========================================================
Гарантия ровно одна и она проверяемая:

    **файл базы данных (и любая его копия — бэкап, снапшот, случайный
    экспорт) сам по себе не содержит читаемого текста участников.**

Гарантии «владелец никогда не увидит» здесь НЕТ и быть не может: сервер
обязан читать текст в открытом виде в момент запроса (собрать промпт,
позвать модель, показать переписку). Ключ шифрования лежит на том же
сервере — вне БД, но на том же диске. Тот, кто управляет процессом и может
править код, расшифрует всё. Подробности и формулировки для сайта —
``docs/MEMBER_ENCRYPTION.md``. Не обещать больше, чем написано там.

ПОЧЕМУ НЕ ``app/vault.py``
-------------------------
Vault (Fernet) требует мастер-пароль на КАЖДУЮ операцию — его вводит человек
руками. Работающий процесс его не имеет, а фоновые задачи тем более. Vault
остаётся тем, чем был: сейф владельца под ручной пароль. Здесь другая задача —
прозрачное шифрование строк, которые пишет и читает сам сервер.

ПОЧЕМУ НЕ ``cryptography``
--------------------------
Пакета НЕТ в обязательных зависимостях (только extra ``backup``) и НЕТ в
текущем окружении. Шифрование, которое тихо превращается в no-op на боевой
машине, хуже отсутствующего: оно даёт ложное обещание. Поэтому здесь только
stdlib (``hmac``/``hashlib``) и стандартная композиция:

    keystream = HMAC-SHA256(enc_key, nonce || counter) блоками по 32 байта
    ciphertext = plaintext XOR keystream                     (режим CTR)
    tag = HMAC-SHA256(mac_key, nonce || ciphertext)[:16]     (encrypt-then-MAC)

``enc_key``/``mac_key`` выводятся из ключа записи и nonce раздельно
(``HMAC(dek, b"E"|nonce)`` / ``HMAC(dek, b"M"|nonce)``), nonce — 16 случайных
байт на каждую запись. Сравнение тега — ``hmac.compare_digest``.

Конверт помечен версией (``pcenc1:``): если когда-нибудь появится AES-GCM,
он станет ``pcenc2:``, а старые строки продолжат читаться.

ГДЕ ЛЕЖИТ КЛЮЧ
--------------
``$PERSONA_DATA_DIR/member_keyring.key`` (по умолчанию ``~/.persona/``) —
рядом с БД, но НЕ в ней, chmod 600. Переопределяется переменной окружения
``PERSONA_MEMBER_KEYRING_KEY`` (base64url 32 байта) — для контейнеров, где
файловая система эфемерна.

    ⚠ ПОТЕРЯ ЭТОГО ФАЙЛА = ПОТЕРЯ ВСЕХ ЗАШИФРОВАННЫХ ДАННЫХ.
    Бэкап базы без него бесполезен; бэкап вместе с ним — это бэкап без
    шифрования. Это осознанный размен, см. docs/MEMBER_ENCRYPTION.md.

ДВУХУРОВНЕВАЯ СХЕМА (envelope)
------------------------------
Мастер-ключ НЕ шифрует данные напрямую. На каждого пользователя и на каждую
ветку переписки заводится свой случайный DEK (32 байта), который лежит в БД
завёрнутым в мастер-ключ (``user_encryption_key`` / ``dm_thread_key``). Это
даёт две вещи: удаление аккаунта каскадом уносит DEK (крипто-шреддинг — даже
уцелевшая в старом бэкапе строка больше не расшифруется), а компрометация
одного DEK не раскрывает чужие.

ДЕГРАДАЦИЯ (важно)
------------------
Модуль НИКОГДА не роняет запрос:
* нет ключа/каталога → пишем открытым текстом и громко логируем (данные не
  теряются, обещание не выполняется — это видно в логах и в /health);
* строка зашифрована, а ключ не подходит → чтение возвращает пустую строку,
  а не 500. Пустой чат честнее упавшего.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Any, Final

from app.logging_setup import get_logger

log = get_logger("persona.member_crypto")

#: Метка конверта. Всё, что с неё начинается, — шифротекст этого модуля.
#: Всё, что без неё, — legacy plaintext (или значение, записанное в момент,
#: когда ключа не было). Отличать одно от другого нужно ВСЕГДА: наполовину
#: зашифрованная таблица без маркера — худший из возможных исходов.
PREFIX: Final[str] = "pcenc1:"

_KEY_BYTES: Final[int] = 32
_NONCE_BYTES: Final[int] = 16
_TAG_BYTES: Final[int] = 16
_KEYRING_ENV: Final[str] = "PERSONA_MEMBER_KEYRING_KEY"  # noqa: S105 — имя переменной
_KEYRING_FILENAME: Final[str] = "member_keyring.key"

#: Скоупы ключей: имя → (таблица, колонка-идентификатор).
_SCOPES: Final[dict[str, tuple[str, str]]] = {
    "user": ("user_encryption_key", "user_id"),
    "dm_thread": ("dm_thread_key", "thread_id"),
}

#: Подстроки в имени ключа ``user_settings``, после которых значение считается
#: секретом (API-ключ, токен бота, пароль SMTP). Список намеренно широкий:
#: лишнее шифрование стоит микросекунды, пропущенный ключ стоит ключа.
#: ``app/auth/data_export.py`` берёт этот же список — редактирование в выгрузке
#: и шифрование в базе обязаны совпадать.
SECRET_KEY_HINTS: Final[tuple[str, ...]] = (
    "api_key",
    "apikey",
    "token",
    "password",
    "passwd",
    "secret",
    "credential",
)

# ── процессные кэши ─────────────────────────────────────────────────────────
# Мастер-ключ читается с диска один раз, DEK'и разворачиваются один раз на
# процесс: иначе выборка из 50 сообщений — это 50 чтений файла и 50 HMAC'ов
# разворота. Ключи и так живут в памяти этого процесса, кэш ничего не
# ослабляет.
_master_key: bytes | None = None
_master_probed: bool = False
_dek_cache: dict[tuple[str, int], bytes] = {}
_MAX_DEK_CACHE = 512


def reset_cache() -> None:
    """Сбросить кэш мастер-ключа и DEK'ов (тесты, смена PERSONA_DATA_DIR)."""
    global _master_key, _master_probed
    _master_key = None
    _master_probed = False
    _dek_cache.clear()


# ---------------------------------------------------------------------------
# Мастер-ключ
# ---------------------------------------------------------------------------


def data_dir() -> Path:
    """Каталог данных Persona (там же, где БД и billing_secrets.json)."""
    raw = os.environ.get("PERSONA_DATA_DIR")
    return Path(raw) if raw else Path.home() / ".persona"


def keyring_path() -> Path:
    return data_dir() / _KEYRING_FILENAME


def _decode_key(raw: str) -> bytes | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        return None
    return key if len(key) == _KEY_BYTES else None


def _load_or_create_master() -> bytes | None:
    """Мастер-ключ: env → файл → создать файл. ``None``, если ничего не вышло.

    Создание — это НОРМАЛЬНЫЙ путь первого запуска, но оно логируется на
    уровне ``warning``: если ключ создаётся во второй раз, значит предыдущий
    потерян, и вместе с ним потеряны все ранее зашифрованные строки.
    """
    from_env = _decode_key(os.environ.get(_KEYRING_ENV, ""))
    if from_env is not None:
        log.info("member_crypto.key.env")
        return from_env

    path = keyring_path()
    try:
        if path.exists():
            key = _decode_key(path.read_text("utf-8"))
            if key is not None:
                return key
            log.error("member_crypto.key.corrupt", path=str(path))
            return None
    except OSError as exc:
        log.error("member_crypto.key.read_failed", error=str(exc))
        return None

    key = secrets.token_bytes(_KEY_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(base64.urlsafe_b64encode(key).decode("ascii"), "utf-8")
        try:
            path.chmod(0o600)  # best-effort: на Windows игнорируется
        except OSError:  # pragma: no cover — платформенное
            pass
    except OSError as exc:
        log.error("member_crypto.key.write_failed", error=str(exc), path=str(path))
        return None
    log.warning("member_crypto.key.created", path=str(path))
    return key


def master_key() -> bytes | None:
    """Мастер-ключ процесса (кэшируется). ``None`` → шифрование недоступно."""
    global _master_key, _master_probed
    if not _master_probed:
        _master_probed = True
        _master_key = _load_or_create_master()
    return _master_key


def encryption_available() -> bool:
    """Есть ли рабочий мастер-ключ. Диагностика/health, а не гейт на запись."""
    return master_key() is not None


# ---------------------------------------------------------------------------
# Примитив: AEAD на HMAC-SHA256 (CTR + encrypt-then-MAC)
# ---------------------------------------------------------------------------


def _subkeys(dek: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    enc = hmac.new(dek, b"E" + nonce, hashlib.sha256).digest()
    mac = hmac.new(dek, b"M" + nonce, hashlib.sha256).digest()
    return enc, mac


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _seal(dek: bytes, plaintext: str) -> str:
    nonce = secrets.token_bytes(_NONCE_BYTES)
    raw = plaintext.encode("utf-8")
    enc_key, mac_key = _subkeys(dek, nonce)
    stream = _keystream(enc_key, nonce, len(raw))
    ciphertext = bytes(a ^ b for a, b in zip(raw, stream, strict=True))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()[:_TAG_BYTES]
    blob = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return PREFIX + blob


def _open(dek: bytes, stored: str) -> str | None:
    """Расшифровать конверт. ``None`` — тег не сошёлся или мусор."""
    body = stored[len(PREFIX) :]
    try:
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None
    if len(blob) < _NONCE_BYTES + _TAG_BYTES:
        return None
    nonce = blob[:_NONCE_BYTES]
    tag = blob[_NONCE_BYTES : _NONCE_BYTES + _TAG_BYTES]
    ciphertext = blob[_NONCE_BYTES + _TAG_BYTES :]
    enc_key, mac_key = _subkeys(dek, nonce)
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()[:_TAG_BYTES]
    if not hmac.compare_digest(tag, expected):
        return None
    stream = _keystream(enc_key, nonce, len(ciphertext))
    raw = bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:  # pragma: no cover — тег уже сошёлся
        return None


# ---------------------------------------------------------------------------
# DEK: ключ на пользователя / на ветку переписки
# ---------------------------------------------------------------------------


def _wrap_key(scope: str, scope_id: int) -> bytes | None:
    """Ключ, которым завёрнут DEK этого скоупа. Привязан к id — чужой не подойдёт."""
    master = master_key()
    if master is None:
        return None
    label = f"persona.member_crypto.v1|{scope}|{int(scope_id)}".encode()
    return hmac.new(master, label, hashlib.sha256).digest()


async def _read_wrapped(conn: Any, scope: str, scope_id: int) -> bytes | None:
    table, column = _SCOPES[scope]
    try:
        cursor = await conn.execute(
            f"SELECT wrapped_key FROM {table} WHERE {column} = ?",  # noqa: S608 — из _SCOPES
            (int(scope_id),),
        )
        row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 — старая БД без миграции 237
        log.debug("member_crypto.dek.read_failed", scope=scope, error=str(exc))
        return None
    if row is None:
        return None
    return bytes(row["wrapped_key"])


async def _create_dek(conn: Any, scope: str, scope_id: int, wrap: bytes) -> bytes | None:
    """Завести DEK. Гонка двух запросов разрешается ``INSERT OR IGNORE`` + перечтением."""
    table, column = _SCOPES[scope]
    dek = secrets.token_bytes(_KEY_BYTES)
    sealed = _seal(wrap, base64.urlsafe_b64encode(dek).decode("ascii"))
    try:
        await conn.execute(
            f"INSERT OR IGNORE INTO {table} ({column}, wrapped_key) VALUES (?, ?)",  # noqa: S608
            (int(scope_id), sealed.encode("utf-8")),
        )
    except Exception as exc:  # noqa: BLE001 — нет таблицы / FK на удалённую строку
        log.warning("member_crypto.dek.create_failed", scope=scope, error=str(exc))
        return None
    stored = await _read_wrapped(conn, scope, scope_id)
    if stored is None:
        return None
    opened = _open(wrap, stored.decode("utf-8"))
    return base64.urlsafe_b64decode(opened) if opened else None


async def _dek(
    scope: str,
    scope_id: int,
    conn: Any | None = None,
    *,
    create: bool = False,
) -> bytes | None:
    """DEK скоупа: кэш → БД → (при ``create``) новый.

    ``conn`` ПЕРЕДАВАТЬ ОБЯЗАТЕЛЬНО, если вызов происходит внутри чужой
    транзакции: своё соединение на запись под открытым ``BEGIN IMMEDIATE``
    упрётся в блокировку.
    """
    cache_key = (scope, int(scope_id))
    cached = _dek_cache.get(cache_key)
    if cached is not None:
        return cached

    wrap = _wrap_key(scope, scope_id)
    if wrap is None:
        return None

    async def _run(active: Any) -> bytes | None:
        stored = await _read_wrapped(active, scope, scope_id)
        if stored is None:
            if not create:
                return None
            return await _create_dek(active, scope, scope_id, wrap)
        opened = _open(wrap, stored.decode("utf-8"))
        if opened is None:
            log.error("member_crypto.dek.unwrap_failed", scope=scope, id=int(scope_id))
            return None
        try:
            return base64.urlsafe_b64decode(opened)
        except (ValueError, TypeError):  # pragma: no cover — тег уже сошёлся
            return None

    if conn is not None:
        dek = await _run(conn)
    else:
        from app.storage.db import get_connection  # noqa: PLC0415 — цикл импорта

        async with get_connection() as own:
            dek = await _run(own)

    if dek is not None:
        if len(_dek_cache) >= _MAX_DEK_CACHE:
            _dek_cache.clear()
        _dek_cache[cache_key] = dek
    return dek


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def is_ciphertext(value: object) -> bool:
    """``True`` для строки-конверта этого модуля (маркер legacy vs encrypted)."""
    return isinstance(value, str) and value.startswith(PREFIX)


def is_secret_setting_key(key: str) -> bool:
    """Секрет ли эта строка ``user_settings`` (ключ API, токен, пароль)."""
    low = (key or "").lower()
    return any(hint in low for hint in SECRET_KEY_HINTS)


async def encrypt(
    scope: str,
    scope_id: int,
    plaintext: str,
    conn: Any | None = None,
) -> str:
    """Зашифровать значение для скоупа. Нет ключа → вернуть открытый текст.

    Молча терять данные из-за отсутствующего ключа нельзя: продукт обязан
    работать. Поэтому невозможность зашифровать — это громкий лог и plaintext,
    а не исключение.
    """
    text = plaintext if isinstance(plaintext, str) else str(plaintext)
    if not text:
        return text
    dek = await _dek(scope, scope_id, conn, create=True)
    if dek is None:
        log.warning("member_crypto.encrypt.unavailable", scope=scope, id=int(scope_id))
        return text
    return _seal(dek, text)


async def decrypt(
    scope: str,
    scope_id: int,
    stored: str | None,
    conn: Any | None = None,
) -> str:
    """Расшифровать значение. Не конверт → вернуть как есть (legacy plaintext).

    Конверт, который не открывается (ключ потерян/подменён), превращается в
    пустую строку: пустое поле честнее и безопаснее, чем 500 или мусор в UI.
    """
    if stored is None:
        return ""
    text = stored if isinstance(stored, str) else str(stored)
    if not is_ciphertext(text):
        return text
    dek = await _dek(scope, scope_id, conn, create=False)
    if dek is None:
        log.error("member_crypto.decrypt.no_key", scope=scope, id=int(scope_id))
        return ""
    opened = _open(dek, text)
    if opened is None:
        log.error("member_crypto.decrypt.failed", scope=scope, id=int(scope_id))
        return ""
    return opened


async def encrypt_for_user(user_id: int, plaintext: str, conn: Any | None = None) -> str:
    return await encrypt("user", int(user_id), plaintext, conn)


async def decrypt_for_user(user_id: int, stored: str | None, conn: Any | None = None) -> str:
    return await decrypt("user", int(user_id), stored, conn)


async def encrypt_for_thread(thread_id: int, plaintext: str, conn: Any | None = None) -> str:
    return await encrypt("dm_thread", int(thread_id), plaintext, conn)


async def decrypt_for_thread(thread_id: int, stored: str | None, conn: Any | None = None) -> str:
    return await decrypt("dm_thread", int(thread_id), stored, conn)


async def encrypts_memory_for(user_id: int) -> bool:
    """Шифруются ли факты ``user_memory`` этого пользователя.

    НЕ шифруются у ВЛАДЕЛЬЦА — и это осознанно. Владелец и есть тот, у кого
    база; шифровать его данные от него самого смысла нет, а цена высокая:
    ``user_memory.text`` напрямую читают SQL-джойны сновидений, проекций и
    графа знаний (``app/adapters/memory/*``, ``app/adapters/projection/*``,
    ``app/knowledge_graph.py``) — все они owner-only. Оставив владельца в
    открытом виде, мы не трогаем ни один из этих путей: там просто не бывает
    чужих строк.
    """
    if not encryption_available():
        return False
    try:
        from app.auth.owner import is_owner  # noqa: PLC0415 — цикл импорта

        return not await is_owner(int(user_id))
    except Exception as exc:  # noqa: BLE001 — резолв владельца не должен ронять запись
        log.debug("member_crypto.owner_probe_failed", error=str(exc))
        return False


__all__ = [
    "PREFIX",
    "SECRET_KEY_HINTS",
    "decrypt",
    "decrypt_for_thread",
    "decrypt_for_user",
    "encrypt",
    "encrypt_for_thread",
    "encrypt_for_user",
    "encryption_available",
    "encrypts_memory_for",
    "is_ciphertext",
    "is_secret_setting_key",
    "keyring_path",
    "master_key",
    "reset_cache",
]
