"""Установщик Windows Scheduled Task для persona_watchdog (S9).

Регистрирует задачу ``PersonaWatchdog`` через ``schtasks``, которая раз в
минуту запускает ``ops/persona_watchdog.py`` тем же pythonw, что и сервер.
Пути НЕ захардкожены — определяются так же, как в самом watchdog (от
расположения файла + env-override), поэтому установщик портативен: на
любой машине ставит задачу с правильными путями.

Использование::

    # из корня репо (или откуда угодно — пути автоопределяются):
    .venv\\Scripts\\python.exe ops\\install_watchdog_windows.py            # установить
    .venv\\Scripts\\python.exe ops\\install_watchdog_windows.py --dry-run  # показать команду
    .venv\\Scripts\\python.exe ops\\install_watchdog_windows.py --uninstall

Env-override (как у watchdog): ``PERSONA_REPO``, ``PERSONA_PYEXE``,
``PERSONA_DATA_DIR``; плюс ``PERSONA_WATCHDOG_TASK`` — имя задачи
(деф. ``PersonaWatchdog``), ``PERSONA_WATCHDOG_INTERVAL`` — период в
минутах (деф. ``1``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = os.environ.get("PERSONA_WATCHDOG_TASK", "PersonaWatchdog")
INTERVAL_MIN = os.environ.get("PERSONA_WATCHDOG_INTERVAL", "1")


def _detect_repo() -> str:
    """Корень репо: env ``PERSONA_REPO`` иначе ops/ → родитель."""
    env = os.environ.get("PERSONA_REPO")
    if env:
        return env
    # ops/install_watchdog_windows.py → parent=ops, parent.parent=корень
    return str(Path(__file__).resolve().parent.parent)


def _detect_pyexe(repo: str) -> str:
    """pythonw из venv репо → pythonw рядом с интерпретатором → sys.executable.

    Та же логика, что в persona_watchdog: pythonw не плодит консольных окон.
    """
    env = os.environ.get("PERSONA_PYEXE")
    if env:
        return env
    venv_pyw = Path(repo) / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pyw.exists():
        return str(venv_pyw)
    sibling = Path(sys.executable).with_name("pythonw.exe")
    if sibling.exists():
        return str(sibling)
    return sys.executable


def _build_command(repo: str, pyexe: str) -> str:
    """Команда задачи: ``"<pyexe>" "<repo>/ops/persona_watchdog.py"``.

    Кавычки обязательны — пути могут содержать пробелы. Путь к скрипту —
    абсолютный, т.к. schtasks не задаёт рабочий каталог (watchdog сам
    выставляет cwd=REPO для uvicorn).
    """
    script = str(Path(repo) / "ops" / "persona_watchdog.py")
    return f'"{pyexe}" "{script}"'


def _schtasks_create(command: str) -> list[str]:
    # /SC MINUTE /MO N — каждые N минут; /F — перезаписать без вопроса;
    # /RL LIMITED — без повышения прав (задача под текущим юзером).
    return [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", command,
        "/SC", "MINUTE",
        "/MO", str(INTERVAL_MIN),
        "/RL", "LIMITED",
        "/F",
    ]


def _schtasks_delete() -> list[str]:
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def _run(cmd: list[str]) -> int:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        print(f"FAILED to run {cmd[0]}: {exc}", file=sys.stderr)
        return 1
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.stderr:
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Persona watchdog Scheduled Task.")
    parser.add_argument("--uninstall", action="store_true", help="удалить задачу")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="показать команду schtasks, ничего не выполняя",
    )
    args = parser.parse_args(argv)

    if os.name != "nt" and not args.dry_run:
        print("Эта команда работает только на Windows (schtasks).", file=sys.stderr)
        return 2

    if args.uninstall:
        cmd = _schtasks_delete()
        if args.dry_run:
            print(subprocess.list2cmdline(cmd))
            return 0
        return _run(cmd)

    repo = _detect_repo()
    pyexe = _detect_pyexe(repo)
    command = _build_command(repo, pyexe)
    cmd = _schtasks_create(command)

    print(f"Task   : {TASK_NAME} (каждые {INTERVAL_MIN} мин)")
    print(f"Repo   : {repo}")
    print(f"PyExe  : {pyexe}")
    print(f"Run    : {command}")

    if args.dry_run:
        print("--- schtasks ---")
        print(subprocess.list2cmdline(cmd))
        return 0

    rc = _run(cmd)
    if rc == 0:
        print(f"\nУстановлено. Управление: schtasks /Query|/Run|/Change|/Delete /TN {TASK_NAME}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
