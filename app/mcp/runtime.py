"""Phase 2 — real MCP runtime: launch stdio MCP servers and route tool calls.

Until now ``app.mcp`` only had *built-in* tools plus a DB registry of MCP
server configs that nothing actually launched (see the stub docstring in
``app/mcp/__init__.py``). This module is the missing runtime:

* :class:`MCPServerProcess` — one configured MCP server, spoken to over
  **JSON-RPC 2.0 on stdio** (the MCP wire protocol). Windows-safe: plain
  ``subprocess.Popen`` + a reader thread, never ``asyncio`` subprocesses
  (which ``NotImplementedError`` under uvicorn's SelectorEventLoop).
  Implements the handshake (``initialize`` → ``notifications/initialized``),
  ``tools/list`` discovery, and ``tools/call`` dispatch.
* :class:`MCPSupervisor` — module-level singleton. Reads the
  ``mcp_runtime_enabled`` kv flag; when on, lazily starts every enabled,
  non-builtin ``mcp_server`` row whose launch command passes the
  **allowlist** (:data:`_ALLOWED_LAUNCHERS`). Aggregates discovered tools
  as ``mcp__{server}__{tool}`` names.
* :func:`discovered_mcp_tools` — names the chat prompt can advertise.
* :func:`call_mcp_tool` — the dispatch hook ``app.mcp.call_tool`` falls
  through to for any ``mcp__*`` name.

Safety posture: MCP servers run arbitrary local commands, so the launcher
allowlist + the master ``mcp_runtime_enabled`` switch are the gates. URL/SSE
transports are intentionally out of scope here (stdio only).
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import threading
import time
from queue import Empty, Queue
from typing import Any

from app.logging_setup import get_logger

log = get_logger("persona.mcp.runtime")

# Only these executables may be spawned as MCP servers. The model can ask to
# "install mcp X" (writes a config row), but a config can never make us run an
# arbitrary binary — the command's first token must be one of these.
_ALLOWED_LAUNCHERS: frozenset[str] = frozenset({
    "npx", "node", "uvx", "uv", "python", "python3", "py",
    "deno", "bun", "pnpm", "dlx",
})

_PREFIX = "mcp__"          # tool-name namespace: mcp__{server}__{tool}
_RPC_TIMEOUT = 60.0        # per-call response wait
_INIT_TIMEOUT = 45.0       # handshake + tools/list
_PROTOCOL_VERSION = "2024-11-05"


def _server_slug(name: str) -> str:
    """Normalise a server name into a tool-namespace token (no '__' / spaces)."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name).strip("_").lower()


async def _load_timeouts() -> dict[int, int]:
    """Пер-серверные timeout_ms одним запросом → {server_id: timeout_ms}.

    Best-effort: колонка timeout_ms (миграция 202) могла не примениться на
    старой БД — тогда возвращаем пустую карту, и runtime берёт дефолтный
    _RPC_TIMEOUT на каждый сервер (поведение как раньше, ничего не ломаем).
    """
    from app.storage.db import get_connection  # noqa: PLC0415

    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT id, timeout_ms FROM mcp_server WHERE timeout_ms IS NOT NULL"
            )
            rows = await cur.fetchall()
        out: dict[int, int] = {}
        for r in rows:
            try:
                val = int(r["timeout_ms"])
            except (TypeError, ValueError):
                continue
            if val > 0:
                out[int(r["id"])] = val
        return out
    except Exception as exc:  # noqa: BLE001 — нет колонки/таблицы → дефолт
        log.debug("mcp.runtime.timeouts_unavailable", error=str(exc))
        return {}


def _launcher_allowed(command: str) -> bool:
    """First token of the launch command must be on the allowlist."""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    if not parts:
        return False
    head = parts[0].lower()
    # Strip a path and .cmd/.exe so 'C:\\...\\npx.cmd' still matches 'npx'.
    head = head.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".cmd", ".exe", ".bat", ".ps1"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    return head in _ALLOWED_LAUNCHERS


class MCPServerProcess:
    """One stdio MCP server subprocess (JSON-RPC 2.0)."""

    def __init__(
        self, server_id: int, name: str, command: str, timeout_ms: int | None = None
    ) -> None:
        self.server_id = server_id
        self.name = name
        self.slug = _server_slug(name)
        self.command = command
        # Пер-серверный таймаут вызова (мс). None/<=0 → дефолтный _RPC_TIMEOUT.
        self.rpc_timeout = (
            float(timeout_ms) / 1000.0
            if (timeout_ms is not None and int(timeout_ms) > 0)
            else _RPC_TIMEOUT
        )
        self.proc: subprocess.Popen[str] | None = None
        self._q: Queue[dict[str, Any]] = Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 0
        self.tools: dict[str, dict[str, Any]] = {}   # tool_name → spec
        self.lock = asyncio.Lock()
        self.error: str | None = None

    # -- blocking pipe primitives (run via to_thread) -------------------
    def _spawn(self) -> bool:
        try:
            parts = shlex.split(self.command, posix=False)
        except ValueError:
            parts = self.command.split()
        try:
            self.proc = subprocess.Popen(  # noqa: S603
                parts,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except (OSError, ValueError) as exc:
            self.error = f"не смог запустить '{self.command}': {exc}"
            return False

        def _pump() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except json.JSONDecodeError:
                    log.debug("mcp.runtime.bad_line", server=self.name, line=line[:120])

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()
        return True

    def _write(self, obj: dict[str, Any]) -> bool:
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def _await_id(self, rid: int, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self._q.get(timeout=1.0)
            except Empty:
                if self.proc is not None and self.proc.poll() is not None:
                    return {"error": {"message": "MCP-процесс завершился"}}
                continue
            if msg.get("id") == rid:
                return msg
            # notifications / other ids — ignore.
        return {"error": {"message": "MCP не ответил вовремя (таймаут)"}}

    def _rpc_blocking(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        if not self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            return {"error": {"message": "не смог отправить запрос MCP"}}
        return self._await_id(rid, timeout)

    def _notify_blocking(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _handshake_blocking(self) -> bool:
        """initialize → initialized → tools/list. Returns True on success."""
        init = self._rpc_blocking(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "persona", "version": "1.0"},
            },
            _INIT_TIMEOUT,
        )
        if "error" in init:
            self.error = f"initialize: {init['error'].get('message', '?')}"
            return False
        self._notify_blocking("notifications/initialized", {})
        listed = self._rpc_blocking("tools/list", {}, _INIT_TIMEOUT)
        if "error" in listed:
            self.error = f"tools/list: {listed['error'].get('message', '?')}"
            return False
        for spec in (listed.get("result", {}) or {}).get("tools", []) or []:
            tname = str(spec.get("name", "")).strip()
            if tname:
                self.tools[tname] = spec
        return True

    def _terminate(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        except OSError:
            pass
        self.proc = None

    # -- async surface --------------------------------------------------
    async def start(self) -> bool:
        if not await asyncio.to_thread(self._spawn):
            return False
        ok = await asyncio.to_thread(self._handshake_blocking)
        if not ok:
            await asyncio.to_thread(self._terminate)
        else:
            log.info("mcp.runtime.started", server=self.name, tools=len(self.tools))
        return ok

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        async with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                return f"[error] MCP-сервер '{self.name}' не запущен"
            resp = await asyncio.to_thread(
                self._rpc_blocking, "tools/call",
                {"name": tool, "arguments": arguments or {}}, self.rpc_timeout,
            )
        if "error" in resp:
            return f"[error] MCP {self.name}.{tool}: {resp['error'].get('message', '?')}"
        result = resp.get("result", {}) or {}
        return _render_tool_result(result)

    async def aclose(self) -> None:
        async with self.lock:
            await asyncio.to_thread(self._terminate)
            self.tools.clear()


def _render_tool_result(result: dict[str, Any]) -> str:
    """Flatten an MCP ``tools/call`` result into LLM-readable text."""
    if result.get("isError"):
        prefix = "[error] "
    else:
        prefix = "[ok] "
    chunks: list[str] = []
    for item in result.get("content", []) or []:
        itype = item.get("type")
        if itype == "text":
            chunks.append(str(item.get("text", "")))
        elif itype in ("image", "audio"):
            chunks.append(f"({itype}: {item.get('mimeType', 'binary')})")
        elif itype == "resource":
            res = item.get("resource", {})
            chunks.append(str(res.get("text") or res.get("uri") or "(resource)"))
    body = "\n".join(c for c in chunks if c) or json.dumps(result, ensure_ascii=False)[:4000]
    return prefix + body


class MCPSupervisor:
    """Singleton owning every running stdio MCP server."""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerProcess] = {}   # slug → process
        self._lock = asyncio.Lock()
        self._started = False

    async def is_enabled(self) -> bool:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv  # noqa: PLC0415

        async with get_connection() as conn:
            return (await get_kv(conn, "mcp_runtime_enabled") or "0").strip() == "1"

    async def ensure_started(self) -> None:
        """Lazily launch enabled, allowlisted, non-builtin MCP servers."""
        if not await self.is_enabled():
            return
        async with self._lock:
            if self._started:
                return
            self._started = True
        from app.mcp.servers import list_servers  # noqa: PLC0415

        # Пер-серверные таймауты (миграция 202). Колонка timeout_ms могла ещё
        # не примениться (старая БД) — тогда тихо работаем на дефолте.
        timeouts = await _load_timeouts()

        for row in await list_servers():
            command = str(row.get("command", ""))
            if not row.get("enabled") or command.startswith("builtin:"):
                continue
            if command.startswith(("http://", "https://", "sse:")):
                log.info("mcp.runtime.skip_url_transport", server=row.get("name"))
                continue
            if not _launcher_allowed(command):
                log.warning("mcp.runtime.launcher_blocked",
                            server=row.get("name"), command=command[:80])
                continue
            sid = int(row["id"])
            proc = MCPServerProcess(sid, str(row["name"]), command, timeouts.get(sid))
            try:
                if await proc.start():
                    async with self._lock:
                        self._servers[proc.slug] = proc
            except Exception as exc:  # noqa: BLE001 — one bad server never blocks others
                log.warning("mcp.runtime.start_failed", server=row.get("name"), error=str(exc))

    async def discovered(self) -> list[str]:
        """All discovered tools as ``mcp__{slug}__{tool}`` names."""
        await self.ensure_started()
        out: list[str] = []
        async with self._lock:
            for slug, proc in self._servers.items():
                out.extend(f"{_PREFIX}{slug}__{t}" for t in proc.tools)
        return out

    async def call(self, namespaced: str, args: dict[str, Any]) -> str:
        await self.ensure_started()
        rest = namespaced[len(_PREFIX):] if namespaced.startswith(_PREFIX) else namespaced
        slug, _, tool = rest.partition("__")
        async with self._lock:
            proc = self._servers.get(slug)
        if proc is None:
            return f"[error] MCP-сервер '{slug}' не запущен или не найден"
        if tool not in proc.tools:
            return f"[error] у MCP '{slug}' нет инструмента '{tool}'"
        return await proc.call(tool, args)

    async def close_all(self) -> None:
        async with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
            self._started = False
        for proc in servers:
            try:
                await proc.aclose()
            except Exception:  # noqa: BLE001, S110
                pass


# Module-level singleton.
_SUPERVISOR = MCPSupervisor()


def get_supervisor() -> MCPSupervisor:
    return _SUPERVISOR


async def discovered_mcp_tools() -> list[str]:
    """Names of MCP tools available right now (empty if runtime is off)."""
    try:
        if not await _SUPERVISOR.is_enabled():
            return []
        return await _SUPERVISOR.discovered()
    except Exception as exc:  # noqa: BLE001 — discovery must never break the chat
        log.warning("mcp.runtime.discover_failed", error=str(exc))
        return []


async def call_mcp_tool(name: str, args: dict[str, Any]) -> str:
    """Dispatch a ``mcp__server__tool`` call. Returns an LLM-facing string."""
    try:
        if not await _SUPERVISOR.is_enabled():
            return "[error] MCP-рантайм выключен (включи на /settings/automation)"
        return await _SUPERVISOR.call(name, args)
    except Exception as exc:  # noqa: BLE001
        log.warning("mcp.runtime.call_failed", tool=name, error=str(exc))
        return f"[error] MCP вызов упал: {type(exc).__name__}: {exc}"


async def shutdown_mcp_runtime() -> None:
    """App shutdown hook — terminate every MCP subprocess."""
    await _SUPERVISOR.close_all()


__all__ = [
    "MCPServerProcess",
    "MCPSupervisor",
    "call_mcp_tool",
    "discovered_mcp_tools",
    "get_supervisor",
    "shutdown_mcp_runtime",
]
