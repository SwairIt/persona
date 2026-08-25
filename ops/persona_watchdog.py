"""Persona watchdog — keep the web server alive (T29).

Run every minute by a Windows Scheduled Task. Probes the local server;
if it's DOWN or HUNG (two checks fail), kills any stale uvicorn and
starts a fresh one. This is the safety net for "сайт ложится" — any
hang/crash auto-recovers within ~1-2 minutes instead of waiting for a
human.

The launched uvicorn gets EXPLICIT ``PERSONA_*`` env so it always uses
the real data dir regardless of which account the scheduled task runs
as — never a fresh/empty DB.

**Рестарт по запросу (T30).** Задача живёт в сессии 0 под S4U/Highest, значит
и порождённый uvicorn повышенный — неповышенный шелл владельца убить его НЕ
может (``taskkill`` рапортует успех и ничего не делает). Поэтому деплой из
обычного терминала не убивает процесс сам, а кладёт маркер
``<PERSONA_DATA_DIR>/restart.request``; watchdog на очередном тике его
валидирует, потребляет и перезапускает сервер сам — уже с правами. Маркер и
все проверки — ``ops/restart_request.py``, хелпер — ``ops/deploy_restart.py``.
Рестарт считается успешным ТОЛЬКО если ``/healthz`` отдаёт версию из
``app/__init__.py``; иначе в лог уходит громкое ``RESTART-REQUEST FAILED``.

Портативность (S9): пути НЕ захардкожены — берутся из env с дефолтами,
которые на текущей машине дают ровно тот же результат, что и раньше:

* ``repo`` — автоопределение от расположения этого файла
  (``Path(__file__).resolve().parent.parent``); override ``PERSONA_REPO``.
* ``PERSONA_PYEXE`` — иначе ``<repo>/.venv/Scripts/pythonw.exe`` если есть,
  иначе ``pythonw.exe`` рядом с ``sys.executable``, иначе ``sys.executable``.
* ``PERSONA_DATA_DIR`` — иначе из ``<repo>/.env`` → ``%USERPROFILE%/.persona``.
* ``PERSONA_WATCHDOG_HOST`` / ``PERSONA_WATCHDOG_PORT`` — деф. ``0.0.0.0`` / ``8000``.
* ``PERSONA_WATCHDOG_FORWARDED_ALLOW_IPS`` — деф. ``192.168.33.3,127.0.0.1``.

``<repo>/.env`` читается (если есть), но ТОЛЬКО как fallback для каталога
данных. Launch-параметры uvicorn (host/port/forwarded) намеренно НЕ берутся
из ``.env`` — это конфиг приложения, не watchdog'а; иначе чужой
``PERSONA_PORT`` в ``.env`` сменил бы порт сервера. Дефолты дают ровно то
же поведение, что и раньше.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:  # запуск скриптом: ops/ уже в sys.path[0]
    import restart_request
except ImportError:  # импорт пакетом (тесты): ops.restart_request
    from ops import restart_request  # type: ignore[no-redef]


# --- репозиторий: от расположения файла (ops/ → корень) -------------------
def _detect_repo() -> str:
    env = os.environ.get("PERSONA_REPO")
    if env:
        return env
    # ops/persona_watchdog.py → parent=ops, parent.parent=корень репо
    return str(Path(__file__).resolve().parent.parent)


def _read_dotenv(repo: str) -> dict[str, str]:
    """Распарсить ``<repo>/.env`` в dict (best-effort, без зависимостей).

    ВАЖНО: НЕ пишем в ``os.environ`` и НЕ трогаем launch-параметры uvicorn
    (host/port/forwarded). ``.env`` — конфиг ПРИЛОЖЕНИЯ; его читает само
    приложение при старте (cwd=repo). Watchdog'у из ``.env`` нужен лишь
    путь к данным (``PERSONA_DATA_DIR``) как fallback для логов/state.
    Так дефолты дают РОВНО текущее поведение: сервер по-прежнему слушает
    свой фиксированный host/port, что бы ни стояло в ``.env`` (там может
    быть, например, иной ``PERSONA_PORT`` для запуска вручную).

    Парсер минимальный: ``KEY=VALUE``, ``#``-комментарии и пустые строки
    игнорируются, кавычки снимаются.
    """
    out: dict[str, str] = {}
    path = Path(repo) / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = val.strip().strip('"').strip("'")
    return out


REPO = _detect_repo()
_DOTENV = _read_dotenv(REPO)


def _detect_pyexe(repo: str) -> str:
    """pythonw из venv репо → pythonw рядом с текущим интерпретатором → sys.executable.

    pythonw (GUI-subsystem) НЕ аллоцирует консоль. С обычным python.exe uv-шим
    при re-exec плодил чёрное консольное окно при каждом респауне (CREATE_NO_WINDOW
    его не давил). pythonw + CREATE_NO_WINDOW = тихий фоновый сервер без окна.
    """
    env = os.environ.get("PERSONA_PYEXE")
    if env:
        return env
    venv_pyw = Path(repo) / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pyw.exists():
        return str(venv_pyw)
    # pythonw рядом с активным интерпретатором (если запущены из python.exe)
    sibling = Path(sys.executable).with_name("pythonw.exe")
    if sibling.exists():
        return str(sibling)
    return sys.executable


def _detect_home() -> str:
    return (
        os.environ.get("USERPROFILE")
        or os.environ.get("HOME")
        or str(Path.home())
    )


def _detect_data_dir(home: str) -> str:
    # Приоритет: реальный env → .env → дефолт ~/.persona. Все три на текущей
    # машине указывают на один и тот же каталог.
    env = os.environ.get("PERSONA_DATA_DIR") or _DOTENV.get("PERSONA_DATA_DIR")
    if env:
        return env
    return str(Path(home) / ".persona")


PYEXE = _detect_pyexe(REPO)
HOME = _detect_home()
# normpath: ``.env`` отдаёт прямые слэши — приводим к нативным, чтобы
# производные пути (логи/state) были с единым разделителем.
PERSONA_DIR = os.path.normpath(_detect_data_dir(HOME))

# Launch-параметры uvicorn — СОБСТВЕННЫЕ watchdog-переменные (НЕ из .env), чтобы
# конфиг приложения не менял то, как watchdog поднимает сервер. Дефолты = ровно
# прежние хардкоды (0.0.0.0:8000 + forwarded для FastPanel на yesbeat).
HOST = os.environ.get("PERSONA_WATCHDOG_HOST", "0.0.0.0")
PORT = os.environ.get("PERSONA_WATCHDOG_PORT", "8000")
# FastPanel на yesbeat (192.168.33.3) проксирует на :8000; localhost для пробы.
FORWARDED_ALLOW_IPS = os.environ.get(
    "PERSONA_WATCHDOG_FORWARDED_ALLOW_IPS", "192.168.33.3,127.0.0.1"
)

URL = f"http://127.0.0.1:{PORT}/landing"
OUT_LOG = os.path.join(PERSONA_DIR, "uvicorn.out.log")
ERR_LOG = os.path.join(PERSONA_DIR, "uvicorn.err.log")
WLOG = os.path.join(PERSONA_DIR, "watchdog.log")

_DETACHED = 0x00000008  # DETACHED_PROCESS
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def _log(msg: str) -> None:
    try:
        with open(WLOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except OSError:
        pass


def _alive() -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=20) as resp:
            return resp.status == 200
    except Exception:
        return False


# Единый фильтр «наш uvicorn». ``app.web.main`` есть только у Persona —
# чужие проекты на этой машине (напр. QuadroFlow на :8123 c ``app.main:app``)
# под него НЕ попадают и не трогаются.
_PS_SERVER_FILTER = (
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
    "Where-Object { $_.CommandLine -like '*app.web.main*' }"
)


def _powershell(script: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )


def _server_pids() -> list[int]:
    """PID'ы живых Persona-uvicorn. Пусто и при ошибке запроса тоже пусто —
    поэтому вызывающий обязан трактовать пустоту вместе с другими признаками
    (HTTP-проба), а не как доказательство само по себе."""
    try:
        proc = _powershell(
            _PS_SERVER_FILTER + " | ForEach-Object { $_.ProcessId }", timeout=45
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _kill_existing() -> None:
    # Kill any python running our uvicorn (hung or duplicate).
    try:
        proc = _powershell(
            _PS_SERVER_FILTER + " | ForEach-Object { taskkill /F /PID $_.ProcessId /T }"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"kill FAILED to run: {exc!r}")
        return
    # taskkill из неповышенного шелла по процессу сессии 0 печатает ошибку в
    # stdout И возвращает 0 — молча «успешно» ничего не убив. Логируем громко.
    # «not found» отфильтровано намеренно: убив дерево через /T, вторая
    # итерация цикла законно не находит уже мёртвого потомка — это НЕ отказ
    # в правах, и в громком логе такому шуму не место.
    noise = " ".join((proc.stdout or "").split() + (proc.stderr or "").split())
    lowered = noise.lower()
    denied = "denied" in lowered or "отказано" in lowered or "access" in lowered
    if denied:
        _log(f"kill DENIED (needs elevation?): {noise[:500]}")


def _start() -> int | None:
    """Поднять uvicorn. Возвращает PID — это доказательство, что процесс НОВЫЙ
    (при рестарте на ту же версию совпадение версии само по себе не доказывает
    ничего: её отдавал бы и старый процесс)."""
    env = os.environ.copy()
    # Pin identity + data dir so the DB is ALWAYS the real one.
    env["USERPROFILE"] = HOME
    env["HOME"] = HOME
    env["PERSONA_DATA_DIR"] = PERSONA_DIR
    env["PERSONA_DB_PATH"] = os.path.join(PERSONA_DIR, "persona.db")
    env["PERSONA_THUMBNAILS_DIR"] = os.path.join(PERSONA_DIR, "thumbnails")
    # T29 — lean mode (no background workers) + multiple web processes.
    # This is the stable config: heavy worker churn off, requests
    # parallelised so heavy page renders don't freeze the whole server.
    env["PERSONA_LEAN_MODE"] = "1"
    # Каталог данных может отсутствовать на свежей машине — логам нужен путь.
    try:
        os.makedirs(PERSONA_DIR, exist_ok=True)
    except OSError:
        pass
    out = open(OUT_LOG, "ab")  # noqa: SIM115 - handed to the detached child
    err = open(ERR_LOG, "ab")  # noqa: SIM115
    # ОДИН процесс (без --workers): pythonw + uvicorn --workers крашит воркеры
    # (multiprocessing-спавн без stdout под pythonw). Single-process async-uvicorn
    # тянет нагрузку одного пользователя и не конфликтует за запись в SQLite.
    proc = subprocess.Popen(
        [
            PYEXE, "-m", "uvicorn", "app.web.main:create_app",
            # 0.0.0.0: слушать и localhost (watchdog-проба 127.0.0.1), и LAN —
            # FastPanel на yesbeat (192.168.33.3) проксирует на :8000.
            # Доступ к :8000 из LAN ограничен firewall-правилом (только yesbeat).
            "--factory", "--host", HOST, "--port", str(PORT),
            "--proxy-headers", "--forwarded-allow-ips", FORWARDED_ALLOW_IPS,
        ],
        cwd=REPO,
        env=env,
        stdout=out,
        stderr=err,
        creationflags=_DETACHED | _NO_WINDOW,
        close_fds=True,
    )
    return proc.pid


STATE_FILE = os.path.join(PERSONA_DIR, "watchdog_state")
# Only restart after the server has been unresponsive for this many
# consecutive minute-runs. Prevents the flapping where a single slow
# probe (load blip) killed a healthy server and triggered a cold-start
# herd. A true hang stays down across runs and recovers after ~N min.
_FAIL_THRESHOLD = 3
_PROBE_TIMEOUT = 20  # generous — a slow page is NOT a dead server


def _read_fails() -> int:
    try:
        return int(open(STATE_FILE, encoding="utf-8").read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_fails(n: int) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(n))
    except OSError:
        pass


# --------------------------------------------------------------------------
# Рестарт по запросу (T30). Неповышенный шелл не может убить наш uvicorn из
# сессии 0 — он кладёт маркер, а мы (уже повышенные) его выполняем.
# Контракт маркера, защита от подделки и от петли — в ops/restart_request.py.
# --------------------------------------------------------------------------
HEALTH_URL = f"http://127.0.0.1:{PORT}/healthz"
#: Сколько ждать, пока свежий сервер начнёт отдавать НУЖНУЮ версию.
_RESTART_VERIFY_SECONDS = 75
_VERSION_IN_INIT = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


def _repo_version() -> str | None:
    """Версия из ``app/__init__.py`` — источник правды о том, что деплоим."""
    try:
        text = (Path(REPO) / "app" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_IN_INIT.search(text)
    return match.group(1) if match else None


def _served_version() -> str | None:
    """Версия, которую отдаёт ЖИВОЙ процесс на порту. ``None`` — не отвечает.

    ``/healthz`` дешёвый, без БД и без авторизации, и возвращает
    ``app.__version__`` того интерпретатора, который реально слушает порт —
    то есть ловит именно «порт живой, но код старый».
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=15) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read(4096).decode("utf-8", "replace"))
    except Exception:
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) and version else None


def _restart_now(expected_version: str | None) -> tuple[bool, str]:
    """Убить + поднять + УБЕДИТЬСЯ, что отдаётся нужная версия.

    Возвращает ``(ok, detail)``. ``ok=False`` — вызывающий обязан заорать в
    лог: «перезапустил» без проверки версии — это и есть тот самый тихий
    отказ, из-за которого сайт месяцами отдаёт старый код.
    """
    before = _server_pids()
    _kill_existing()
    time.sleep(3)
    remaining = _server_pids()
    if remaining:
        _log(f"kill left PIDs alive {remaining} (were {before}) — retrying once")
        _kill_existing()
        time.sleep(4)
        remaining = _server_pids()
    if remaining:
        return False, (
            f"could not kill uvicorn PIDs {remaining} — they still hold :{PORT}. "
            "This needs an ELEVATED shell."
        )
    pid = _start()
    # ВАЖНО: pid от Popen — это лаунчер venv'а (pythonw-шим), а слушает порт
    # его потомок. Поэтому доказательством считаем СМЕНУ множества PID'ов, а
    # не сам pid шима.
    started = f"launcher pid {pid}" if pid else "launched"
    deadline = time.time() + _RESTART_VERIFY_SECONDS
    detail = f"{started} but server never answered /healthz"
    while time.time() < deadline:
        time.sleep(4)
        served = _served_version()
        if served is None:
            continue
        if expected_version and served != expected_version:
            detail = f"{started} but serving {served}, expected {expected_version}"
            continue
        after = _server_pids()
        return True, f"pids {after} (were {before}), serving version {served}"
    return False, detail


def _handle_restart_request() -> bool:
    """Обработать маркер рестарта. ``True`` — запрос был принят и отработан."""
    try:
        decision = restart_request.consume(PERSONA_DIR, REPO)
    except Exception as exc:  # noqa: BLE001 - маркер не должен ронять watchdog
        _log(f"RESTART-REQUEST ERROR reading marker: {exc!r}")
        return False

    if decision.status == "none":
        return False
    if not decision.accepted:
        _log(f"RESTART-REQUEST IGNORED ({decision.reason}) — marker discarded")
        restart_request.write_result(
            PERSONA_DIR, nonce=decision.nonce, status="ignored", detail=decision.reason
        )
        return False

    payload = decision.payload or {}
    nonce = decision.nonce
    wanted = payload.get("version")
    if not isinstance(wanted, str):
        wanted = None
    # Версия в маркере — лишь то, чего ждал деплой; сверяемся с рабочей копией.
    on_disk = _repo_version()
    if wanted and on_disk and wanted != on_disk:
        _log(
            f"RESTART-REQUEST nonce={nonce} asked for {wanted} but app/__init__.py "
            f"says {on_disk} — restarting to {on_disk}"
        )
    expected = on_disk or wanted
    _log(f"RESTART-REQUEST ACCEPTED nonce={nonce} target={expected} — restarting")
    restart_request.write_result(
        PERSONA_DIR, nonce=nonce, status="running", detail=f"target {expected}"
    )
    try:
        ok, detail = _restart_now(expected)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"restart raised {exc!r}"
    if ok:
        _log(f"RESTART-REQUEST OK nonce={nonce} — {detail}")
        _write_fails(0)
    else:
        # Громко: это единственное место, где видно «рестарт не сработал».
        _log(f"RESTART-REQUEST FAILED nonce={nonce} — {detail}")
    restart_request.write_result(
        PERSONA_DIR, nonce=nonce, status="ok" if ok else "failed", detail=detail
    )
    return True


def main() -> None:
    # Явный запрос на рестарт обрабатывается ПЕРВЫМ: сервер может быть жив и
    # отвечать — просто старым кодом, и обычные пробы этого не заметят.
    if _handle_restart_request():
        return
    # Two probes this run, 8s apart — ride out a brief blip without
    # counting it as a failure.
    if _alive() or (time.sleep(8) or _alive()):
        _write_fails(0)
        return
    fails = _read_fails() + 1
    _write_fails(fails)
    if fails < _FAIL_THRESHOLD:
        _log(f"server not responding ({fails}/{_FAIL_THRESHOLD}) — NOT restarting yet")
        return
    _log(f"server DOWN {fails} runs (~{fails} min) — restarting")
    _kill_existing()
    time.sleep(3)
    _start()
    for _ in range(8):
        time.sleep(4)
        if _alive():
            _log("restart OK — server responding")
            _write_fails(0)
            return
    _log("restart attempted but server still not responding after ~32s")


if __name__ == "__main__":
    main()
