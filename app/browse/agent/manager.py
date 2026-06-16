"""Phase 2 — per-session browser-agent supervisor (Windows-safe).

Owns at most one persistent Playwright worker subprocess per chat session
(:mod:`app.browse.agent.worker`). The web server talks to the worker over
plain ``Popen`` pipes plus a background reader thread — we deliberately do
NOT use ``asyncio.create_subprocess_*`` because it raises
``NotImplementedError`` on Windows under uvicorn's SelectorEventLoop.

Guarantees baked in here (defence-in-depth):

* **Domain allow/deny** — ``open`` is refused for localhost / RFC1918 /
  link-local / reserved IPs (SSRF protection: the browser must not be a
  pivot into the server's own network). An optional allowlist (kv
  ``browser_allow_domains``) narrows it further; a denylist (kv
  ``browser_deny_domains``) blocks specific hosts.
* **Step cap** — each session may issue at most ``MAX_STEPS`` commands;
  beyond that the session is force-closed so a runaway model can't drive
  the browser forever.
* **Idle TTL** — a reaper closes sessions idle longer than ``IDLE_TTL``.
* **Single-flight** — one command at a time per session (a per-session
  lock), matching the worker's single-threaded request loop.

All blocking pipe I/O is pushed onto worker threads via
``asyncio.to_thread`` so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.parse import urlparse

from app.logging_setup import get_logger

log = get_logger("persona.browse.agent")

# Tunables (small, conservative — interactive use, not a crawler).
IDLE_TTL: float = 300.0          # close sessions idle longer than this (s)
MAX_STEPS: int = 60              # hard cap on commands per session
_REQUEST_TIMEOUT: float = 80.0   # per-command response wait (covers nav)
_READY_TIMEOUT: float = 90.0     # worker startup (Chromium cold-start)

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Domain policy (allow/deny + RFC1918 / localhost block).
# ---------------------------------------------------------------------------
def _host_is_private(host: str) -> bool:
    """True if ``host`` resolves to (or literally is) a non-public address."""
    h = host.lower().strip("[]")
    if h in ("localhost", "0.0.0.0", "::1", "127.0.0.1"):  # noqa: S104
        return True
    # Literal IP?
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass
    # Hostname → resolve and check every A/AAAA record.
    try:
        for _fam, _t, _p, _c, sockaddr in socket.getaddrinfo(h, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except OSError:
        # DNS failure → let it through; the browser will report the error.
        return False
    return False


async def _domain_policy() -> tuple[set[str], set[str]]:
    """Read optional allow/deny domain lists from kv. Empty allow = allow-all
    (still subject to the private-network block)."""
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415

    def _split(raw: str | None) -> set[str]:
        if not raw:
            return set()
        return {
            part.strip().lower()
            for part in raw.replace(",", "\n").splitlines()
            if part.strip()
        }

    async with get_connection() as conn:
        allow = _split(await get_kv(conn, "browser_allow_domains"))
        deny = _split(await get_kv(conn, "browser_deny_domains"))
    return allow, deny


def _host_matches(host: str, patterns: set[str]) -> bool:
    """Suffix-match host against a set of domain patterns (``example.com``
    matches ``www.example.com``)."""
    host = host.lower()
    return any(host == p or host.endswith("." + p) for p in patterns)


async def check_url(url: str) -> tuple[bool, str, str]:
    """Validate a navigation target. → (ok, normalised_url, reason_if_blocked)."""
    raw = (url or "").strip()
    if not raw:
        return False, "", "нужен url"
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        u = urlparse(raw)
    except ValueError:
        return False, raw, "плохой URL"
    if u.scheme not in ("http", "https"):
        return False, raw, "только http/https"
    host = u.hostname or ""
    if not host:
        return False, raw, "нет хоста в URL"
    if _host_is_private(host):
        return False, raw, "локальная/частная сеть запрещена (localhost/RFC1918)"
    allow, deny = await _domain_policy()
    if deny and _host_matches(host, deny):
        return False, raw, f"домен в чёрном списке: {host}"
    if allow and not _host_matches(host, allow):
        return False, raw, f"домен не в белом списке: {host} (см. /settings/automation)"
    return True, raw, ""


# ---------------------------------------------------------------------------
# Session = one worker subprocess + reader thread + bookkeeping.
# ---------------------------------------------------------------------------
class _BrowserSession:
    """Wraps a single ``worker.py`` subprocess for one chat session."""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self.proc: subprocess.Popen[str] | None = None
        self._q: Queue[dict[str, Any]] = Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 0
        self.steps = 0
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()
        self._ready = False

    # -- lifecycle ------------------------------------------------------
    def _spawn(self) -> dict[str, Any]:
        """Blocking: launch the worker and wait for its readiness banner.

        Runs inside ``asyncio.to_thread``. Returns the banner dict
        (``{"event":"ready"}`` or ``{"event":"fatal",...}``)."""
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "app.browse.agent.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,  # line-buffered
            cwd=str(_REPO_ROOT),
        )

        def _pump() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except json.JSONDecodeError:
                    log.debug("browse.agent.bad_line", session=self.session_id, line=line[:120])

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()

        # Wait for the first banner.
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                msg = self._q.get(timeout=1.0)
            except Empty:
                if self.proc.poll() is not None:
                    return {"event": "fatal", "error": "worker завершился при старте"}
                continue
            if msg.get("event") == "ready":
                self._ready = True
                return msg
            if msg.get("event") == "fatal":
                return msg
        return {"event": "fatal", "error": "worker не стал готов вовремя"}

    def _send_recv(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Blocking: write one request, drain queue until the matching id."""
        if self.proc is None or self.proc.stdin is None:
            return {"ok": False, "error": "browser-сессия не запущена"}
        if self.proc.poll() is not None:
            return {"ok": False, "error": "browser-процесс умер"}
        rid = payload["id"]
        try:
            self.proc.stdin.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            return {"ok": False, "error": f"не смог отправить команду: {exc}"}
        deadline = time.monotonic() + _REQUEST_TIMEOUT
        while time.monotonic() < deadline:
            try:
                msg = self._q.get(timeout=1.0)
            except Empty:
                if self.proc.poll() is not None:
                    return {"ok": False, "error": "browser-процесс завершился"}
                continue
            if msg.get("id") == rid:
                return msg
            # stray banner / out-of-order — ignore and keep draining.
        return {"ok": False, "error": "browser не ответил вовремя (таймаут)"}

    def _terminate(self) -> None:
        """Blocking: best-effort graceful → forced shutdown."""
        if self.proc is None:
            return
        try:
            if self.proc.stdin and self.proc.poll() is None:
                self.proc.stdin.write('{"id":-1,"cmd":"close"}\n')
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    # -- public (async) -------------------------------------------------
    async def ensure_started(self) -> str | None:
        """Spawn the worker if needed. Returns an error string or None."""
        if self.proc is not None and self.proc.poll() is None and self._ready:
            return None
        banner = await asyncio.to_thread(self._spawn)
        if banner.get("event") != "ready":
            return f"[error] браузер не запустился: {banner.get('error', '?')}"
        return None

    async def command(self, cmd: str, **kw: Any) -> dict[str, Any]:
        """Send one command, enforcing the per-session step cap."""
        async with self.lock:
            self.last_used = time.monotonic()
            if self.steps >= MAX_STEPS:
                return {"ok": False, "step_cap": True,
                        "error": f"достигнут лимит шагов браузера ({MAX_STEPS}) — сессия закрыта"}
            self._next_id += 1
            self.steps += 1
            payload: dict[str, Any] = {"id": self._next_id, "cmd": cmd, **kw}
            return await asyncio.to_thread(self._send_recv, payload)

    async def aclose(self) -> None:
        async with self.lock:
            await asyncio.to_thread(self._terminate)
            self._ready = False


# ---------------------------------------------------------------------------
# Registry of live sessions + idle reaper.
# ---------------------------------------------------------------------------
_SESSIONS: dict[int, _BrowserSession] = {}
_SESSIONS_LOCK = asyncio.Lock()
_REAPER_TASK: asyncio.Task[None] | None = None


async def _get_session(session_id: int) -> _BrowserSession:
    async with _SESSIONS_LOCK:
        sess = _SESSIONS.get(session_id)
        if sess is None:
            sess = _BrowserSession(session_id)
            _SESSIONS[session_id] = sess
        _ensure_reaper()
        return sess


def _ensure_reaper() -> None:
    global _REAPER_TASK  # noqa: PLW0603
    if _REAPER_TASK is None or _REAPER_TASK.done():
        try:
            _REAPER_TASK = asyncio.ensure_future(_reaper_loop())
        except RuntimeError:  # no running loop (tests) — skip
            _REAPER_TASK = None


async def _reaper_loop() -> None:
    """Close sessions idle past IDLE_TTL. Self-terminates when none remain."""
    try:
        while True:
            await asyncio.sleep(30.0)
            now = time.monotonic()
            stale: list[int] = []
            async with _SESSIONS_LOCK:
                for sid, sess in _SESSIONS.items():
                    if now - sess.last_used > IDLE_TTL or sess.steps >= MAX_STEPS:
                        stale.append(sid)
            for sid in stale:
                await close_session(sid)
            async with _SESSIONS_LOCK:
                if not _SESSIONS:
                    return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — reaper must never crash the app
        log.warning("browse.agent.reaper_failed", error=str(exc))


async def close_session(session_id: int) -> bool:
    """Close + forget one session. Idempotent. Returns True if one existed."""
    async with _SESSIONS_LOCK:
        sess = _SESSIONS.pop(session_id, None)
    if sess is None:
        return False
    try:
        await sess.aclose()
    except Exception as exc:  # noqa: BLE001
        log.debug("browse.agent.close_failed", session=session_id, error=str(exc))
    return True


async def close_all() -> None:
    """Shutdown hook — terminate every live worker."""
    async with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for sess in sessions:
        try:
            await sess.aclose()
        except Exception:  # noqa: BLE001, S110
            pass


async def run(session_id: int, cmd: str, **kw: Any) -> dict[str, Any]:
    """Top-level entry: ensure the session's worker is up, then run ``cmd``.

    ``cmd == 'close'`` tears the session down. Returns the worker's
    response dict (or ``{"ok": False, "error": ...}``)."""
    if cmd == "close":
        existed = await close_session(session_id)
        return {"ok": True, "closed": existed}
    sess = await _get_session(session_id)
    err = await sess.ensure_started()
    if err:
        # Failed to boot — drop the dead session so a retry re-spawns.
        await close_session(session_id)
        return {"ok": False, "error": err}
    res = await sess.command(cmd, **kw)
    # Step cap exhausted → proactively close so the next turn is clean.
    if res.get("step_cap"):
        await close_session(session_id)
    return res


__all__ = [
    "IDLE_TTL",
    "MAX_STEPS",
    "check_url",
    "close_all",
    "close_session",
    "run",
]
