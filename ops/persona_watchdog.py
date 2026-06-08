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
        with urllib.request.urlopen(URL, timeout=20) as resp:
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
    # T29 — lean mode (no background workers) + multiple web processes.
    # This is the stable config: heavy worker churn off, requests
    # parallelised so heavy page renders don't freeze the whole server.
    env["PERSONA_LEAN_MODE"] = "1"
    out = open(OUT_LOG, "ab")  # noqa: SIM115 - handed to the detached child
    err = open(ERR_LOG, "ab")  # noqa: SIM115
    subprocess.Popen(
        [
            PYEXE, "-m", "uvicorn", "app.web.main:create_app",
            "--factory", "--host", "127.0.0.1", "--port", "8000",
            "--workers", "3",
        ],
        cwd=REPO,
        env=env,
        stdout=out,
        stderr=err,
        creationflags=_DETACHED | _NO_WINDOW,
        close_fds=True,
    )


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


def main() -> None:
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
