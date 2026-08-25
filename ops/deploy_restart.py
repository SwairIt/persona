"""Деплой из НЕПОВЫШЕННОГО шелла: попросить рестарт и честно проверить версию.

Зачем
-----
``PersonaWatchdog`` крутится в сессии 0 (``S4U`` + ``RunLevel=Highest``) —
благодаря этому сайт переживает логаут владельца. Обратная сторона: uvicorn,
который watchdog поднял, тоже повышенный, и обычный терминал владельца его
**не убьёт**: ``taskkill /F /T`` вернёт «успех», процесс останется жив, порт
занят, сайт продолжит отдавать СТАРЫЙ код. Скрипт закрывает именно этот
провал: он ничего не убивает сам, а кладёт валидируемый маркер, ждёт, пока
повышенный watchdog выполнит рестарт, и **не рапортует успех, пока по HTTP не
увидит нужную версию**.

Использование
-------------
    .venv\\Scripts\\python.exe ops\\deploy_restart.py
    .venv\\Scripts\\python.exe ops\\deploy_restart.py --timeout 300
    .venv\\Scripts\\python.exe ops\\deploy_restart.py --force      # даже если версия уже та
    .venv\\Scripts\\python.exe ops\\deploy_restart.py --status     # только проверка, без запроса

Код возврата: ``0`` — сайт отдаёт версию из ``app/__init__.py``; ``1`` — нет
(и тогда в выводе печатается точная команда для повышенного шелла).

Что именно проверяется
----------------------
Две независимые проверки, обе обязаны сойтись с ``app/__init__.py``:

* ``/healthz`` → ``version`` — версия ПРОЦЕССА, который реально слушает порт;
* ``?v=`` в HTML ``/landing`` — то, что получит браузер (cache-busting).

Порт сам по себе не доказывает НИЧЕГО: осиротевший uvicorn на унаследованном
сокете держит :8000 и отдаёт код прошлой версии.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "ops"))

# Консоль на этой машине бывает cp866/cp1251: одна «→» в выводе роняла бы весь
# деплой UnicodeEncodeError'ом. Кодировку консоли не подменяем (иначе кириллица
# превратится в кашу) — просто не падаем на том, что она не умеет.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):
        pass

import restart_request  # импорт после sys.path выше - так и задумано

_VERSION_IN_INIT = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")
_VERSION_IN_HTML = re.compile(r"\?v=([0-9A-Za-z.\-+_]+)")

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
TASK_NAME = "PersonaWatchdog"

ELEVATED_FALLBACK = r"""
    # PowerShell, ЗАПУЩЕННЫЙ ОТ ИМЕНИ АДМИНИСТРАТОРА:
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like '*app.web.main*' } |
        ForEach-Object { taskkill /F /PID $_.ProcessId /T }
    Start-ScheduledTask -TaskName PersonaWatchdog
"""


# --------------------------------------------------------------------------
def repo_version(repo: Path = REPO) -> str:
    text = (repo / "app" / "__init__.py").read_text(encoding="utf-8")
    match = _VERSION_IN_INIT.search(text)
    if not match:
        raise SystemExit(f"не нашёл __version__ в {repo / 'app' / '__init__.py'}")
    return match.group(1)


def data_dir(repo: Path = REPO) -> Path:
    """Каталог данных Persona: env → ``<repo>/.env`` → ``~/.persona``.

    Тот же порядок, что и в ``ops/persona_watchdog.py`` — маркер обязан лечь
    ровно туда, куда watchdog смотрит.
    """
    value = os.environ.get("PERSONA_DATA_DIR")
    if not value:
        try:
            for raw in (repo / ".env").read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("PERSONA_DATA_DIR=") and "=" in line:
                    value = line.partition("=")[2].strip().strip('"').strip("'")
                    break
        except OSError:
            value = None
    if not value:
        home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
        value = str(Path(home) / ".persona")
    return Path(os.path.normpath(value))


def _get(url: str, timeout: float = 10.0) -> tuple[int, str] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(400_000).decode("utf-8", "replace")
    except Exception:
        return None


def served_health_version(base_url: str) -> str | None:
    """Версия процесса, который слушает порт (из ``/healthz``)."""
    got = _get(f"{base_url.rstrip('/')}/healthz")
    if not got or got[0] != 200:
        return None
    try:
        payload = json.loads(got[1])
    except (ValueError, TypeError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) and version else None


def served_asset_versions(base_url: str) -> set[str]:
    """Все ``?v=`` из HTML ``/landing`` — то, что реально уедет в браузер."""
    got = _get(f"{base_url.rstrip('/')}/landing", timeout=20.0)
    if not got or got[0] != 200:
        return set()
    return set(_VERSION_IN_HTML.findall(got[1]))


def verify(base_url: str, expected: str) -> tuple[bool, str]:
    """Обе проверки. Успех — только если ОБЕ сошлись с ``expected``."""
    health = served_health_version(base_url)
    if health is None:
        return False, "сервер не отвечает на /healthz"
    if health != expected:
        return False, f"/healthz отдаёт {health}, ожидалась {expected}"
    assets = served_asset_versions(base_url)
    if not assets:
        return False, "в HTML /landing нет ни одного ?v= (страница не отдалась?)"
    stale = sorted(assets - {expected})
    if stale:
        return False, f"в /landing остались ?v={', '.join(stale)} вместо {expected}"
    return True, f"/healthz={health}, ?v={expected}"


def preflight() -> tuple[bool, str]:
    """Собрать приложение в отдельном процессе, НИЧЕГО не рестартуя.

    Рестарт по запросу убивает живой сервер до того, как выяснится, что
    рабочая копия не импортируется, — и сайт остаётся лежать. Дешевле сначала
    построить ``create_app()`` рядом (~15 с) и при поломке просто НЕ класть
    маркер: старая версия продолжает отдаваться.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "from app.web.main import create_app; create_app()"],
            cwd=str(REPO), capture_output=True, text=True, timeout=180, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"не смог запустить проверку сборки: {exc!r}"
    if proc.returncode == 0:
        return True, "рабочая копия собирается (create_app OK)"
    tail = " ".join(((proc.stderr or "") + (proc.stdout or "")).splitlines()[-6:])
    return False, f"create_app() упал (код {proc.returncode}): {tail[:600]}"


def nudge_watchdog() -> str:
    """Пнуть задачу, чтобы не ждать до минуты естественного тика.

    Право «запустить» есть у владельца задачи и БЕЗ повышения. Не вышло —
    ничего страшного: маркер всё равно подхватит очередной тик.
    """
    try:
        proc = subprocess.run(
            ["schtasks", "/Run", "/TN", TASK_NAME],  # noqa: S607 - schtasks из PATH системы
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"не смог запустить schtasks ({exc!r}) — ждём обычный тик"
    if proc.returncode == 0:
        return "задача PersonaWatchdog запущена вне очереди"
    detail = " ".join(((proc.stdout or "") + " " + (proc.stderr or "")).split())
    return f"schtasks /Run не сработал ({detail[:200]}) — ждём обычный тик (до 1 мин)"


def _result_for(dirpath: Path, nonce: str) -> dict[str, Any] | None:
    result = restart_request.read_result(dirpath)
    if result and result.get("nonce") == nonce:
        return result
    return None


def _fail(expected: str, why: str, base_url: str) -> int:
    print("")
    print(f"ПРОВАЛ: сайт НЕ отдаёт {expected} — {why}")
    print("")
    served = served_health_version(base_url)
    if served:
        print(f"  сейчас на {base_url} живёт версия {served} (сайт НЕ лежит)")
    else:
        print(f"  сейчас {base_url} не отвечает вовсе — проверь ~/.persona/uvicorn.err.log")
    print("  лог watchdog'а: ~/.persona/watchdog.log (строки RESTART-REQUEST)")
    print("")
    print("  Сделать вручную — нужен ПОВЫШЕННЫЙ шелл:")
    print(ELEVATED_FALLBACK)
    return 1


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Попросить повышенный watchdog перезапустить сайт и проверить версию",
    )
    parser.add_argument("--timeout", type=float, default=240.0,
                        help="сколько секунд ждать (деф. 240; маркер протухает за 300)")
    parser.add_argument("--url", default=os.environ.get("PERSONA_DEPLOY_URL", DEFAULT_BASE_URL),
                        help=f"база для проверки (деф. {DEFAULT_BASE_URL})")
    parser.add_argument("--force", action="store_true",
                        help="просить рестарт даже если версия уже совпала")
    parser.add_argument("--status", action="store_true",
                        help="только проверить, что отдаётся; ничего не просить")
    parser.add_argument("--no-nudge", action="store_true",
                        help="не дёргать schtasks /Run, ждать естественный тик")
    parser.add_argument("--no-preflight", action="store_true",
                        help="не проверять create_app() перед запросом (быстрее, опаснее)")
    args = parser.parse_args(argv)

    expected = repo_version()
    dirpath = data_dir()
    base_url = args.url.rstrip("/")
    print(f"репозиторий {REPO} -> версия {expected}")
    print(f"каталог данных {dirpath}")

    ok, detail = verify(base_url, expected)
    if args.status:
        print(("OK: " if ok else "НЕ СОВПАЛО: ") + detail)
        return 0 if ok else 1
    if ok and not args.force:
        print(f"OK: {base_url} уже отдаёт {expected} ({detail}) — рестарт не нужен.")
        print("    Нужен принудительный рестарт — добавь --force.")
        return 0
    print(f"сейчас: {detail}")

    if not args.no_preflight:
        built, why = preflight()
        print(f"  preflight: {why}")
        if not built:
            print("")
            print("ОТМЕНА: маркер НЕ положен — рабочая копия не собирается.")
            print(f"        Сайт продолжает отдавать то, что отдавал ({base_url} не тронут).")
            return 2

    payload = restart_request.write_request(dirpath, str(REPO), expected)
    nonce = str(payload["nonce"])
    marker = restart_request.marker_path(dirpath)
    print(f"запрос на рестарт положен: {marker} (nonce {nonce[:8]}...)")
    if not args.no_nudge:
        print(f"  {nudge_watchdog()}")

    # Ждём ИМЕННО вердикт watchdog'а по нашему nonce, а не «версия совпала».
    # Совпадение версии — необходимое условие, но не достаточное: при --force
    # (и вообще всегда, когда рестартуем на ту же версию) её отдаёт ещё СТАРЫЙ
    # процесс, и «успех» через 4 секунды означал бы ровно ничего.
    deadline = time.time() + args.timeout
    seen_running = False
    verdict: dict[str, Any] | None = None
    while time.time() < deadline:
        time.sleep(3)
        result = _result_for(dirpath, nonce)
        if not result:
            continue
        status = str(result.get("status") or "")
        if status == "running":
            if not seen_running:
                seen_running = True
                print("  watchdog взял запрос в работу...")
            continue
        if status in ("ok", "failed", "ignored"):
            verdict = result
            break

    if verdict is None:
        picked_up = "watchdog взял запрос, но не доложил результат" if seen_running else (
            "watchdog так и не подобрал маркер — тикает ли задача? "
            "Get-ScheduledTaskInfo -TaskName PersonaWatchdog"
        )
        return _fail(
            expected,
            f"не дождался за {int(args.timeout)}s ({picked_up})",
            base_url,
        )

    status = str(verdict.get("status") or "")
    detail_text = str(verdict.get("detail") or "")
    if status == "ignored":
        return _fail(expected, f"watchdog отбросил маркер: {detail_text}", base_url)
    if status == "failed":
        return _fail(expected, f"watchdog доложил провал: {detail_text}", base_url)

    print(f"  watchdog доложил: {detail_text}")
    # Даже после «ok» от watchdog'а проверяем сами — его слово не доказательство.
    ok, detail = verify(base_url, expected)
    if not ok:
        return _fail(expected, f"watchdog сказал ok, но по HTTP {detail}", base_url)
    print("")
    print(f"OK: {base_url} отдаёт {expected} ({detail})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
