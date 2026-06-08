"""Persona Mac agent CLI — `persona-agent` console_script entry point.

Subcommands:

* ``persona-agent pair --server URL --token TOK [--config PATH]``
  Write ``~/.config/persona-agent.toml`` (mode 0600) and exit. Verifies
  the server URL parses + that the token is non-empty; does **not** call
  the server.

* ``persona-agent run [--config PATH]``
  Start the long-running daemon.  This is the entry point invoked by the
  LaunchAgent ``com.persona.agent``.

* ``persona-agent status [--config PATH]``
  GET ``/api/agent/me`` and pretty-print the JSON (last-seen times,
  pending uploads, etc.).

* ``persona-agent pause``  /  ``persona-agent resume``
  Touch / delete ``~/.persona-agent.paused``.  The running daemon polls
  for this file each loop iteration.  These commands work even when the
  daemon is offline — the next start picks up the flag.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
import httpx

from config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_PAUSE_FILE,
    AgentConfig,
    load_config,
    write_config,
)
from persona_agent import run as run_daemon

if TYPE_CHECKING:
    from collections.abc import Sequence


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _echo_error(msg: str) -> None:
    """Print ``msg`` in red on stderr without raising."""
    click.echo(click.style(f"[persona-agent] {msg}", fg="red"), err=True)


def _echo_info(msg: str) -> None:
    click.echo(click.style(f"[persona-agent] {msg}", fg="cyan"))


def _load_or_die(config_path: Path | None) -> AgentConfig:
    try:
        return load_config(config_path)
    except FileNotFoundError as exc:
        _echo_error(str(exc))
        sys.exit(2)


def _server_pid_file() -> Path:
    """Where ``run`` drops its PID for ``pause``/``resume`` SIGUSR1 fast-path."""
    return Path.home() / ".persona-agent.pid"


# --------------------------------------------------------------------------- #
# Root group
# --------------------------------------------------------------------------- #


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="persona-agent", message="%(prog)s %(version)s")
def cli() -> None:
    """Persona Mac capture agent — daemon control + status."""


# --------------------------------------------------------------------------- #
# pair
# --------------------------------------------------------------------------- #


@cli.command()
@click.option(
    "--server",
    "server_url",
    required=True,
    help="Persona server URL, e.g. https://persona.example.com",
)
@click.option(
    "--token",
    required=True,
    help="Pairing token issued by the server's /admin/agents page.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Where to write the config file (default {DEFAULT_CONFIG_PATH}).",
)
@click.option(
    "--hostname",
    default=None,
    help="Hostname to report to the server (default = local hostname).",
)
def pair(
    server_url: str,
    token: str,
    config_path: Path | None,
    hostname: str | None,
) -> None:
    """Write the agent config to disk so `run` can pick it up."""
    if not server_url.startswith(("http://", "https://")):
        _echo_error(f"server URL must start with http(s)://, got {server_url!r}")
        sys.exit(2)
    if len(token) < 8:
        _echo_error("pairing token looks suspiciously short (<8 chars); refusing to write")
        sys.exit(2)
    target = write_config(
        server_url=server_url,
        token=token,
        path=config_path,
        hostname=hostname or socket.gethostname(),
    )
    _echo_info(f"wrote config -> {target}")
    _echo_info("next steps:")
    click.echo("  persona-agent run          # start the daemon")
    click.echo("  persona-agent status       # confirm the server sees us")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override config file path.",
)
def run(config_path: Path | None) -> None:
    """Start the capture daemon (blocks until SIGTERM/SIGINT)."""
    # Drop a PID file so external pause/resume can SIGUSR1 us directly.
    pid_file = _server_pid_file()
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - read-only $HOME edge case
        _echo_error(f"could not write PID file {pid_file}: {exc}")

    try:
        exit_code = run_daemon(config_path)
    finally:
        with contextlib.suppress(OSError):  # pragma: no cover - $HOME edge case
            pid_file.unlink(missing_ok=True)
    sys.exit(exit_code)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override config file path.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print the raw /api/agent/me response.",
)
def status(config_path: Path | None, as_json: bool) -> None:
    """Show today's upload totals + last-seen times by calling /api/agent/stats."""
    config = _load_or_die(config_path)
    url = str(config.server.url).rstrip("/") + "/api/agent/stats"
    headers = {
        "Authorization": f"Bearer {config.server.token.get_secret_value()}",
        "User-Agent": "persona-agent/1.15 (cli status)",
    }
    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        _echo_error(f"failed to reach {url}: {exc}")
        sys.exit(3)

    if response.status_code == 401:
        _echo_error("server rejected token (401). Re-pair with `persona-agent pair`.")
        sys.exit(4)
    if response.status_code >= 400:
        _echo_error(f"server error {response.status_code}: {response.text[:300]}")
        sys.exit(5)

    payload: dict[str, object]
    try:
        payload = response.json()
    except ValueError:
        _echo_error(f"server returned non-JSON body: {response.text[:200]}")
        sys.exit(5)

    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    audio_bytes_today = payload.get("audio_bytes_today", 0)
    if isinstance(audio_bytes_today, int):
        audio_human = f"{audio_bytes_today / 1024:.1f} KB"
    else:
        audio_human = str(audio_bytes_today)

    _echo_info(f"connected to {config.server.url}")
    click.echo(f"  agent_id        : {payload.get('agent_id', '—')}")
    click.echo(f"  today (UTC)     : {payload.get('today_utc', '—')}")
    click.echo(f"  screens today   : {payload.get('screens_today', 0)}")
    click.echo(f"  audio today     : {payload.get('audio_segments_today', 0)} segments, {audio_human}")
    click.echo(f"  last_seen_at    : {payload.get('last_seen_at', '—')}")
    click.echo(f"  last_screen_at  : {payload.get('last_screen_at', '—')}")
    click.echo(f"  last_audio_at   : {payload.get('last_audio_at', '—')}")
    paused_local = DEFAULT_PAUSE_FILE.exists()
    click.echo(f"  paused (local)  : {paused_local}")


# --------------------------------------------------------------------------- #
# pause / resume
# --------------------------------------------------------------------------- #


def _signal_running_daemon() -> bool:
    """Best-effort SIGUSR1 to the PID file. Silently no-op on Windows."""
    pid_file = _server_pid_file()
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return False
    try:
        os.kill(pid, sigusr1)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@cli.command()
def pause() -> None:
    """Pause capture by touching ~/.persona-agent.paused (and SIGUSR1)."""
    DEFAULT_PAUSE_FILE.touch()
    _signal_running_daemon()
    _echo_info(f"paused — created {DEFAULT_PAUSE_FILE}")


@cli.command()
def resume() -> None:
    """Resume capture by deleting ~/.persona-agent.paused (and SIGUSR1)."""
    DEFAULT_PAUSE_FILE.unlink(missing_ok=True)
    _signal_running_daemon()
    _echo_info(f"resumed — removed {DEFAULT_PAUSE_FILE}")


# --------------------------------------------------------------------------- #
# Entry point used by pyproject.toml ``persona-agent = "cli:main"``
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    """Click ``standalone_mode=False`` so we can return an int exit code."""
    try:
        cli.main(args=list(argv) if argv is not None else None, standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return int(code) if isinstance(code, int) else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
