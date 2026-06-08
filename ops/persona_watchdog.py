"""Persona watchdog — keep the web server alive (T29).

Run every minute by a Windows Scheduled Task. Probes the local server;
if it's DOWN or HUNG (two checks fail), kills any stale uvicorn and
starts a fresh one. This is the safety net for "сайт ложится" — any
hang/crash auto-recovers within ~1-2 minutes instead of waiting for a
human.

The launched uvicorn gets EXPLICIT ``PERSONA_*`` env so it always uses
the real data dir (C:\\Users\\Yaroslav\\.persona) regardless of which
account the scheduled task runs as — never a fresh/empty DB.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import time
import urllib.request

REPO = r"C:\www-Yaroslav\Persona"
PYEXE = r"C:\www-Yaroslav\Persona\.venv\Scripts\python.exe"
HOME = r"C:\Users\Yaroslav"
PERSONA_DIR = r"C:\Users\Yaroslav\.persona"
URL = "http://127.0.0.1:8000/landing"
OUT_LOG = r"C:\Users\Yaroslav\.persona\uvicorn.out.log"
ERR_LOG = r"C:\Users\Yaroslav\.persona\uvicorn.err.log"
WLOG = r"C:\Users\Yaroslav\.persona\watchdog.log"

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
        with urllib.request.urlopen(URL, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _kill_existing() -> None:
    # Kill any python running our uvicorn (hung or duplicate).
    subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*app.web.main*' } | "
            "ForEach-Object { taskkill /F /PID $_.ProcessId /T }",
        ],
        capture_output=True, text=True, timeout=60,
    )


def _start() -> None:
    env = os.environ.copy()
    # Pin identity + data dir so the DB is ALWAYS the real one.
    env["USERPROFILE"] = HOME
    env["HOME"] = HOME
    env["PERSONA_DATA_DIR"] = PERSONA_DIR
    env["PERSONA_DB_PATH"] = os.path.join(PERSONA_DIR, "persona.db")
    env["PERSONA_THUMBNAILS_DIR"] = os.path.join(PERSONA_DIR, "thumbnails")
    out = open(OUT_LOG, "ab")  # noqa: SIM115 - handed to the detached child
    err = open(ERR_LOG, "ab")  # noqa: SIM115
    subprocess.Popen(
        [
            PYEXE, "-m", "uvicorn", "app.web.main:create_app",
            "--factory", "--host", "127.0.0.1", "--port", "8000",
        ],
        cwd=REPO,
        env=env,
        stdout=out,
        stderr=err,
        creationflags=_DETACHED | _NO_WINDOW,
        close_fds=True,
    )


def main() -> None:
    if _alive():
        return
    # One retry 6s apart so a momentary blip doesn't trigger a restart.
    time.sleep(6)
    if _alive():
        return
    _log("server DOWN/HUNG — restarting")
    _kill_existing()
    time.sleep(3)
    _start()
    # give it time to bind, then confirm
    for _ in range(8):
        time.sleep(4)
        if _alive():
            _log("restart OK — server responding")
            return
    _log("restart attempted but server still not responding after ~32s")


if __name__ == "__main__":
    main()
