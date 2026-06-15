"""Cross-platform support layer for the Persona agent.

The agent runs on macOS AND Windows from one codebase. Every OS-specific
fork lives here so the rest of the agent stays platform-agnostic.

NOTE: this module is deliberately NOT named ``platform.py`` — that would
shadow the stdlib ``platform`` module that persona_agent.py imports for the
User-Agent string. Keep it stdlib-only (no third-party imports) so it loads
everywhere, even before capture deps are installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from shutil import which

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# CREATE_NO_WINDOW keeps spawned PowerShell/console processes from flashing a
# window when the agent runs headless via pythonw.exe. 0 on POSIX → harmless.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Persona"
    return Path.home() / ".config"


def config_path() -> Path:
    """Windows: %APPDATA%\\Persona\\config.toml — mac: ~/.config/persona-agent.toml."""
    return config_dir() / ("config.toml" if IS_WINDOWS else "persona-agent.toml")


def log_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Persona" / "Logs"
    return Path.home() / "Library" / "Logs"


def pause_file() -> Path:
    if IS_WINDOWS:
        return config_dir() / "agent.paused"
    return Path.home() / ".persona-agent.paused"


def pid_file() -> Path:
    if IS_WINDOWS:
        return config_dir() / "agent.pid"
    return Path.home() / ".persona-agent.pid"


def default_fs_roots() -> list[str]:
    """Documented fallback when the server supplies no allowlist roots."""
    return [r"%USERPROFILE%\Projects" if IS_WINDOWS else "~/Projects"]


# --------------------------------------------------------------------------- #
# Notifications (advisory — never raise)
# --------------------------------------------------------------------------- #
def notify(title: str, message: str) -> None:
    try:
        safe_t = title.replace('"', "").replace("'", "").replace("\n", " ")[:80]
        safe_m = message.replace('"', "").replace("'", "").replace("\n", " ")[:200]
        if IS_MACOS:
            script = (
                f'display notification "{safe_m}" with title '
                f'"Persona Agent" subtitle "{safe_t}"'
            )
            subprocess.run(
                ["osascript", "-e", script], timeout=3, check=False, capture_output=True
            )
        elif IS_WINDOWS:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.Visible=$true;"
                f'$n.ShowBalloonTip(4000,"Persona: {safe_t}","{safe_m}",'
                "[System.Windows.Forms.ToolTipIcon]::Info)"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                timeout=4,
                check=False,
                capture_output=True,
                creationflags=_NO_WINDOW,
            )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Shell exec (fs op=exec)
# --------------------------------------------------------------------------- #
_MAC_SHELLS = {"bash", "zsh", "sh"}
_BLOCK_MAC = ("rm -rf /", "rm -rf ~", "mkfs", "shutdown", "reboot", "dd if=", "sudo rm")
_BLOCK_WIN = (
    "remove-item -recurse",
    "rm -rf",
    "format ",
    "format-volume",
    "clear-disk",
    "rmdir /s",
    "del /s",
    "del /f /s",
    "rd /s",
    "shutdown",
)


def exec_shell(
    shell_hint: str, command: str, *, cwd: Path | None = None, timeout: float = 30.0
) -> tuple[str, str]:
    """Run one command; return (status, formatted_output). Cross-platform.

    On Windows always uses PowerShell (pwsh if available, else Windows
    PowerShell). On macOS/Linux uses the requested shell (bash/zsh/sh).
    """
    low = (command or "").lower()
    for bad in (_BLOCK_WIN if IS_WINDOWS else _BLOCK_MAC):
        if bad in low:
            return ("error", f"[error] заблокировано (опасная команда): {bad}")

    if IS_WINDOWS:
        exe = "pwsh" if which("pwsh") else "powershell"
        argv = [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        sh = shell_hint if shell_hint in _MAC_SHELLS else "bash"
        argv = [sh, "-lc", command]

    try:
        cp = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            check=False,
            capture_output=True,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return ("error", f"[error] команда превысила {timeout:.0f}с — убита")
    except Exception as exc:  # noqa: BLE001
        return ("error", f"[error] {type(exc).__name__}: {exc}")

    out = cp.stdout.decode("utf-8", "replace")[:4000] if cp.stdout else ""
    err = cp.stderr.decode("utf-8", "replace")[:2000] if cp.stderr else ""
    parts: list[str] = []
    if out:
        parts.append("STDOUT:\n" + out)
    if err:
        parts.append("STDERR:\n" + err)
    parts.append(f"exit_code: {cp.returncode}")
    return ("done", "[ok]\n" + "\n\n".join(parts))


# --------------------------------------------------------------------------- #
# Text-to-speech (so the AI's `say "..."` works on every OS)
# --------------------------------------------------------------------------- #
def tts_speak(text: str, voice: str | None = None) -> tuple[str, str]:
    try:
        if IS_MACOS:
            argv = ["say"] + (["-v", voice] if voice else []) + [text]
            subprocess.run(argv, timeout=60, check=False, capture_output=True)
        elif IS_WINDOWS:
            safe = (text or "").replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech;"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                + (f"$s.SelectVoice('{voice}');" if voice else "")
                + f"$s.Speak('{safe}')"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                timeout=60,
                check=False,
                capture_output=True,
                creationflags=_NO_WINDOW,
            )
        else:
            return ("error", "[error] TTS не поддерживается на этой ОС")
        return ("done", f"[ok] произнёс {len(text)} символов")
    except Exception as exc:  # noqa: BLE001
        return ("error", f"[error] TTS: {exc}")
