"""Установщик Windows Scheduled Tasks для Persona (S9).

ЭТОТ ФАЙЛ — ТОНКАЯ ОБЁРТКА. Вся правда о задачах живёт в
``ops/install_persona_autostart_windows.ps1``; здесь только привычный
python-CLI поверх него.

ПОЧЕМУ так. Раньше этот скрипт звал ``schtasks /Create ... /RL LIMITED``,
что даёт принципала ``LogonType=InteractiveToken`` — «выполнять только для
вошедшего пользователя». При выходе владельца из Windows-сессии задача
останавливается, watchdog умирает, а вместе с ним и порождённый им uvicorn:
сайт ложится до следующего логина. Ровно этот баг и переустанавливался
заново при каждом прогоне установщика.

Правильная конфигурация (S4U-принципал без пароля + AtStartup-триггер +
StartWhenAvailable) собирается только PowerShell-скриптом — ``schtasks``
не умеет S4U без хранения пароля. Поэтому здесь мы делегируем.

ВАЖНО: установка ТРЕБУЕТ ЗАПУСКА ОТ АДМИНИСТРАТОРА. Неадминский шелл
получает ``Access is denied`` (0x80070005) ровно на трёх вещах:
``LogonType=S4U``, ``RunLevel=Highest`` и ``BootTrigger``. Подробности,
проверка и откат — ``docs/ALWAYS_ON_WINDOWS.md``.

Использование::

    .venv\\Scripts\\python.exe ops\\install_watchdog_windows.py            # установить
    .venv\\Scripts\\python.exe ops\\install_watchdog_windows.py --dry-run  # показать план
    .venv\\Scripts\\python.exe ops\\install_watchdog_windows.py --uninstall

Env-override: ``PERSONA_REPO``, ``PERSONA_PYEXE`` (их читает и .ps1),
``PERSONA_WATCHDOG_INTERVAL`` — период watchdog в минутах (деф. ``1``),
``PERSONA_MEMPROC_INTERVAL`` — период memproc в минутах (деф. ``10``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

WATCHDOG_INTERVAL_MIN = os.environ.get("PERSONA_WATCHDOG_INTERVAL", "1")
MEMPROC_INTERVAL_MIN = os.environ.get("PERSONA_MEMPROC_INTERVAL", "10")


def _detect_repo() -> str:
    """Корень репо: env ``PERSONA_REPO`` иначе ops/ → родитель."""
    env = os.environ.get("PERSONA_REPO")
    if env:
        return env
    return str(Path(__file__).resolve().parent.parent)


def _installer_ps1(repo: str) -> Path:
    return Path(repo) / "ops" / "install_persona_autostart_windows.ps1"


def _build_cmd(repo: str, *, uninstall: bool, dry_run: bool) -> list[str]:
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(_installer_ps1(repo)),
        "-WatchdogIntervalMinutes", str(WATCHDOG_INTERVAL_MIN),
        "-MemprocIntervalMinutes", str(MEMPROC_INTERVAL_MIN),
    ]
    if uninstall:
        cmd.append("-Uninstall")
    if dry_run:
        cmd.append("-DryRun")
    return cmd


def _run(cmd: list[str]) -> int:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        print(f"FAILED to run {cmd[0]}: {exc}", file=sys.stderr)
        return 1
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.stderr:
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the Persona always-on Scheduled Tasks (needs an elevated shell).",
    )
    parser.add_argument("--uninstall", action="store_true", help="удалить обе задачи")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="показать, что будет сделано, ничего не выполняя",
    )
    args = parser.parse_args(argv)

    repo = _detect_repo()
    ps1 = _installer_ps1(repo)
    if not ps1.exists():
        print(f"Не найден установщик: {ps1}", file=sys.stderr)
        return 1

    cmd = _build_cmd(repo, uninstall=args.uninstall, dry_run=args.dry_run)

    if os.name != "nt":
        print("Эта команда работает только на Windows (Scheduled Tasks).", file=sys.stderr)
        print(subprocess.list2cmdline(cmd))
        return 2

    print(f"Repo : {repo}")
    print(f"Via  : {ps1}")
    print(f"Cmd  : {subprocess.list2cmdline(cmd)}")
    print("-" * 60)
    return _run(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
