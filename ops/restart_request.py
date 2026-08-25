"""Restart-on-request marker — unprivileged deploy, elevated restart.

Почему это вообще нужно
-----------------------
``PersonaWatchdog`` теперь живёт в **сессии 0** под ``LogonType=S4U`` +
``RunLevel=Highest`` (см. ``docs/ALWAYS_ON_WINDOWS.md``) — именно поэтому
сайт переживает логаут владельца. Побочный эффект: uvicorn, который watchdog
породил, тоже повышенный и в сессии 0, а **неповышенный** шелл владельца
(обычный терминал, git-хук, агент) его убить НЕ может: ``taskkill /F /T``
возвращает «успех», процесс живёт, порт занят, сайт продолжает отдавать
СТАРЫЙ код. Это ровно тот отказ, о котором предупреждает раздел «Осиротевший
uvicorn» — только теперь он был бы постоянным.

Решение без новой привилегированной поверхности: неповышенный процесс
**просит** о рестарте — кладёт файл-маркер в каталог данных Persona, — а уже
повышенный watchdog на очередном тике маркер валидирует, потребляет и
перезапускает сервер сам. Ни службы, ни новой задачи, ни хранимого пароля,
ни единого открытого порта.

Устройство маркера
------------------
``<PERSONA_DATA_DIR>/restart.request`` — JSON, ровно один путь, никаких
альтернативных мест. Каталог данных лежит внутри профиля владельца, поэтому
писать туда может только он (+ SYSTEM/админы). Watchdog не «перезапускается
на любой подвернувшийся файл», а проверяет:

* **место** — только точный путь выше; файл где-то ещё не существует для него;
* **владельца файла** — SID владельца маркера обязан совпасть с SID владельца
  каталога данных (best-effort: если SID не определяется — проверка молча
  пропускается, чтобы не сломать не-NTFS/не-Windows сценарии);
* **возраст** — и ``requested_at`` из тела, и mtime файла должны укладываться
  в ``MAX_AGE_SECONDS``; «из будущего» тоже отвергается;
* **содержимое** — точный ``kind``, тот же ``repo``, разумная ``version``,
  корректный ``nonce``; размер ограничен ``MAX_MARKER_BYTES``;
* **повтор** — ``nonce`` попадает в журнал ``restart.request.seen``, повторное
  появление того же маркера отвергается как replay.

Идемпотентность и защита от петли рестартов
-------------------------------------------
``consume()`` удаляет маркер **до** того, как watchdog начнёт что-либо
перезапускать, и удаляет его в любом исходе — принят он или отвергнут. Значит:

* принятый однажды маркер не может сработать дважды;
* битый/протухший маркер не будет пережёвываться каждую минуту вечно;
* если удаление вдруг не прошло (файл залочен) — на следующем тике тот же
  ``nonce`` отвергнет журнал, а не «перезапустим ещё раз»;
* упавший на середине деплой оставляет маркер, который просто **протухнет**
  через ``MAX_AGE_SECONDS``, а не будет вечно рестартовать сайт;
* ``MIN_INTERVAL_SECONDS`` — ещё один предохранитель: два принятых запроса
  подряд быстрее этого интервала невозможны.

Модуль намеренно без зависимостей и без ввода-вывода в сеть: его импортируют
и watchdog (``ops/persona_watchdog.py``), и хелпер деплоя
(``ops/deploy_restart.py``), и тесты (``tests/test_restart_request.py``,
без всякого повышения прав).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- имена файлов в каталоге данных ---------------------------------------
MARKER_NAME = "restart.request"
LEDGER_NAME = "restart.request.seen"
RESULT_NAME = "restart.result"

# --- контракт маркера ------------------------------------------------------
KIND = "persona-restart-request/1"
#: Старше этого маркер считается протухшим (рухнувший деплой не должен
#: рестартить сайт вечно). Должно быть ЗАМЕТНО больше периода тика watchdog'а
#: (1 мин) и больше дефолтного ожидания хелпера (240 с).
MAX_AGE_SECONDS = 300.0
#: Допуск на рассинхрон часов; всё, что дальше в будущем, — подделка/мусор.
MAX_FUTURE_SKEW_SECONDS = 60.0
#: Маркер — три строки JSON. Больше — не читаем вовсе.
MAX_MARKER_BYTES = 4096
#: Минимальный зазор между двумя ПРИНЯТЫМИ запросами (анти-дребезг).
MIN_INTERVAL_SECONDS = 15.0
#: Сколько nonce'ов помнить для защиты от повтора.
LEDGER_KEEP = 20

_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-+_]{0,31}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{16,64}$")


def marker_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / MARKER_NAME


def ledger_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / LEDGER_NAME


def result_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / RESULT_NAME


def normalise_repo(repo: str | os.PathLike[str]) -> str:
    """Путь к репо в каноничном виде — для сравнения без сюрпризов регистра."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(repo))))


@dataclass(frozen=True)
class Decision:
    """Вердикт по маркеру.

    ``status``: ``"none"`` — маркера нет; ``"accept"`` — валиден, потреблён,
    надо перезапускать; ``"reject"`` — что-то не так, маркер выброшен.
    """

    status: str
    reason: str
    payload: dict[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accept"

    @property
    def nonce(self) -> str | None:
        if not self.payload:
            return None
        value = self.payload.get("nonce")
        return value if isinstance(value, str) else None


# --------------------------------------------------------------------------
# Владелец файла (Windows). Best-effort: не смогли определить — не мешаем.
# --------------------------------------------------------------------------
_OWNER_SECURITY_INFORMATION = 0x00000001


def file_owner_sid(path: str | os.PathLike[str]) -> str | None:
    """SID владельца файла строкой (``S-1-5-…``) или ``None``.

    Через ctypes/advapi32, чтобы не тащить pywin32 в watchdog. Любая осечка —
    ``None``: вызывающий трактует это как «проверка недоступна», а не как
    «проверка провалена».
    """
    if os.name != "nt":
        return None
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None
    try:
        target = ctypes.c_wchar_p(str(path))
        needed = ctypes.c_ulong(0)
        advapi32.GetFileSecurityW(
            target, _OWNER_SECURITY_INFORMATION, None, 0, ctypes.byref(needed)
        )
        if needed.value == 0:
            return None
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetFileSecurityW(
            target, _OWNER_SECURITY_INFORMATION, buf, needed.value, ctypes.byref(needed)
        ):
            return None
        psid = ctypes.c_void_p()
        defaulted = ctypes.c_int()
        if not advapi32.GetSecurityDescriptorOwner(
            buf, ctypes.byref(psid), ctypes.byref(defaulted)
        ):
            return None
        as_string = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(as_string)):
            return None
        try:
            return as_string.value
        finally:
            kernel32.LocalFree(ctypes.cast(as_string, ctypes.c_void_p))
    except (OSError, AttributeError, ValueError):
        return None


def _owner_verdict(marker: Path, data_dir: Path) -> tuple[bool, str]:
    """(ок?, причина). Неопределимый SID — «ок», проверка просто недоступна."""
    marker_sid = file_owner_sid(marker)
    dir_sid = file_owner_sid(data_dir)
    if marker_sid is None or dir_sid is None:
        return True, "owner check unavailable"
    if marker_sid != dir_sid:
        return False, f"owner mismatch: marker {marker_sid} != data dir {dir_sid}"
    return True, "owner ok"


# --------------------------------------------------------------------------
# Журнал потреблённых запросов
# --------------------------------------------------------------------------
def read_ledger(data_dir: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        raw = ledger_path(data_dir).read_text(encoding="utf-8")
    except OSError:
        return {"recent": [], "last_accepted_at": 0.0}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {"recent": [], "last_accepted_at": 0.0}
    if not isinstance(data, dict):
        return {"recent": [], "last_accepted_at": 0.0}
    recent = data.get("recent")
    if not isinstance(recent, list):
        recent = []
    try:
        last = float(data.get("last_accepted_at") or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    return {"recent": recent, "last_accepted_at": last}


def _remember(data_dir: str | os.PathLike[str], nonce: str, now: float) -> None:
    ledger = read_ledger(data_dir)
    recent = [item for item in ledger["recent"] if isinstance(item, list) and len(item) == 2]
    recent.append([nonce, now])
    payload = {"recent": recent[-LEDGER_KEEP:], "last_accepted_at": now}
    _atomic_write_json(ledger_path(data_dir), payload)


def _seen_nonces(ledger: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for item in ledger["recent"]:
        if isinstance(item, list) and item and isinstance(item[0], str):
            out.add(item[0])
    return out


# --------------------------------------------------------------------------
# Запись запроса (сторона неповышенного деплоя)
# --------------------------------------------------------------------------
def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def build_payload(
    repo: str | os.PathLike[str],
    version: str,
    *,
    reason: str = "deploy",
    now: float | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": KIND,
        "repo": str(repo),
        "version": version,
        "reason": reason,
        "requested_at": time.time() if now is None else now,
        "requested_by": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "nonce": nonce or secrets.token_hex(16),
    }


def write_request(
    data_dir: str | os.PathLike[str],
    repo: str | os.PathLike[str],
    version: str,
    *,
    reason: str = "deploy",
    now: float | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Положить маркер атомарно (tmp + ``os.replace``). Вернуть тело."""
    payload = build_payload(repo, version, reason=reason, now=now, nonce=nonce)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    _atomic_write_json(marker_path(data_dir), payload)
    return payload


# --------------------------------------------------------------------------
# Валидация (чистая функция — её и гоняют тесты)
# --------------------------------------------------------------------------
def validate(
    raw: bytes | str,
    *,
    repo: str | os.PathLike[str],
    now: float,
    mtime: float | None = None,
    seen_nonces: set[str] | None = None,
    last_accepted_at: float = 0.0,
    owner_ok: bool = True,
    owner_reason: str = "owner ok",
    max_age: float = MAX_AGE_SECONDS,
    min_interval: float = MIN_INTERVAL_SECONDS,
) -> Decision:
    """Решить судьбу содержимого маркера. Никакого ввода-вывода."""
    if not owner_ok:
        return Decision("reject", owner_reason)
    if isinstance(raw, bytes):
        if len(raw) > MAX_MARKER_BYTES:
            return Decision("reject", f"oversized marker ({len(raw)} bytes)")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return Decision("reject", "marker is not valid UTF-8")
    else:
        text = raw
        if len(text.encode("utf-8", "ignore")) > MAX_MARKER_BYTES:
            return Decision("reject", "oversized marker")

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return Decision("reject", "marker is not valid JSON")
    if not isinstance(payload, dict):
        return Decision("reject", "marker JSON is not an object")

    if payload.get("kind") != KIND:
        return Decision("reject", f"unexpected kind {payload.get('kind')!r}")

    marker_repo = payload.get("repo")
    if not isinstance(marker_repo, str) or not marker_repo:
        return Decision("reject", "missing repo")
    if normalise_repo(marker_repo) != normalise_repo(repo):
        return Decision("reject", f"repo mismatch: {marker_repo!r}")

    version = payload.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        return Decision("reject", f"bad version {version!r}")

    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not _NONCE_RE.match(nonce):
        return Decision("reject", "bad nonce")

    requested_at = payload.get("requested_at")
    if not isinstance(requested_at, (int, float)) or isinstance(requested_at, bool):
        return Decision("reject", "missing requested_at")
    age = now - float(requested_at)
    if age > max_age:
        return Decision("reject", f"stale request ({int(age)}s old)", payload)
    if age < -MAX_FUTURE_SKEW_SECONDS:
        return Decision("reject", f"requested_at is in the future ({int(-age)}s)", payload)

    # mtime — независимая от тела проверка возраста: подсунуть старый файл со
    # свежим requested_at не поможет.
    if mtime is not None:
        file_age = now - float(mtime)
        if file_age > max_age:
            return Decision("reject", f"stale marker file ({int(file_age)}s old)", payload)

    if seen_nonces and nonce in seen_nonces:
        return Decision("reject", "replay: nonce already consumed", payload)

    since_last = now - float(last_accepted_at or 0.0)
    if last_accepted_at and 0 <= since_last < min_interval:
        return Decision("reject", f"cooldown: {int(since_last)}s since last restart", payload)

    return Decision("accept", "valid request", payload)


# --------------------------------------------------------------------------
# Потребление (сторона watchdog'а)
# --------------------------------------------------------------------------
def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def consume(
    data_dir: str | os.PathLike[str],
    repo: str | os.PathLike[str],
    *,
    now: float | None = None,
    max_age: float = MAX_AGE_SECONDS,
    min_interval: float = MIN_INTERVAL_SECONDS,
) -> Decision:
    """Прочитать, проверить и **выбросить** маркер. Ничего не перезапускает.

    Маркер удаляется при любом исходе, а принятый nonce уходит в журнал ДО
    того, как вызывающий начнёт рестарт: даже если рестарт упадёт на середине,
    второй раз тот же запрос не сработает.
    """
    now = time.time() if now is None else now
    path = marker_path(data_dir)
    try:
        stat = path.stat()
    except OSError:
        return Decision("none", "no marker")

    if stat.st_size > MAX_MARKER_BYTES:
        _discard(path)
        return Decision("reject", f"oversized marker ({stat.st_size} bytes)")
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_MARKER_BYTES + 1)
    except OSError as exc:
        _discard(path)
        return Decision("reject", f"cannot read marker: {exc}")

    owner_ok, owner_reason = _owner_verdict(path, Path(data_dir))
    ledger = read_ledger(data_dir)
    decision = validate(
        raw,
        repo=repo,
        now=now,
        mtime=stat.st_mtime,
        seen_nonces=_seen_nonces(ledger),
        last_accepted_at=float(ledger["last_accepted_at"]),
        owner_ok=owner_ok,
        owner_reason=owner_reason,
        max_age=max_age,
        min_interval=min_interval,
    )

    # Выбрасываем ВСЕГДА — иначе битый маркер жуётся каждую минуту, а принятый
    # даёт петлю рестартов.
    _discard(path)
    if decision.accepted and decision.nonce:
        _remember(data_dir, decision.nonce, now)
    return decision


# --------------------------------------------------------------------------
# Результат последнего запроса — чтобы хелпер видел вердикт watchdog'а
# --------------------------------------------------------------------------
def write_result(
    data_dir: str | os.PathLike[str],
    *,
    nonce: str | None,
    status: str,
    detail: str = "",
    now: float | None = None,
) -> None:
    payload = {
        "nonce": nonce,
        "status": status,
        "detail": detail,
        "at": time.time() if now is None else now,
    }
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        _atomic_write_json(result_path(data_dir), payload)
    except OSError:
        pass


def read_result(data_dir: str | os.PathLike[str]) -> dict[str, Any] | None:
    try:
        raw = result_path(data_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
