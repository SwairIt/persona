"""T25 (2026-06-08) — Persona built-in tools (no Node.js / npm required).

These plug into the same ``mcp_server`` registry as real MCP servers
but execute as plain Python coroutines instead of subprocesses. Each
tool has:
  * a stable ``name`` that the LLM sees in its system prompt
  * a JSON-schema-ish ``parameters`` blob (for the prompt)
  * an async ``run(args: dict) -> str`` function

Safety: read-only tools are safest. Write/shell tools must be
explicitly enabled in /admin/mcp by the user — the runtime checks
``mcp_server.enabled`` before dispatching.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

log = get_logger("persona.mcp.builtin")


# Hard sandbox: never let the model touch system directories. Reads
# allowed anywhere; writes/shell forbidden inside these.
_FORBIDDEN_WRITE_PREFIXES = (
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
    "/etc", "/usr", "/bin", "/sbin", "/boot",
)


def _is_safe_write_path(path: str) -> bool:
    p = os.path.abspath(path)
    return not any(p.startswith(prefix) for prefix in _FORBIDDEN_WRITE_PREFIXES)


async def read_file(args: dict[str, Any]) -> str:
    path = str(args.get("path", "")).strip()
    if not path:
        return "[error] path required"
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"[error] не существует: {p}"
        if p.is_dir():
            return f"[error] это директория, не файл: {p}"
        if p.stat().st_size > 500_000:
            return f"[error] файл больше 500 КБ ({p.stat().st_size} байт). Не открываю."
        content = p.read_text(encoding="utf-8", errors="replace")
        return f"[ok] {p}\n```\n{content}\n```"
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"


async def list_dir(args: dict[str, Any]) -> str:
    path = str(args.get("path", ".")).strip() or "."
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"[error] не существует: {p}"
        if not p.is_dir():
            return f"[error] это файл, не директория: {p}"
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        lines = [f"[ok] {p}", ""]
        for e in entries[:200]:
            tag = "DIR " if e.is_dir() else "FILE"
            size = "" if e.is_dir() else f" {e.stat().st_size}b"
            lines.append(f"  {tag} {e.name}{size}")
        if len(entries) > 200:
            lines.append(f"  … +{len(entries) - 200} ещё")
        return "\n".join(lines)
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"


async def write_file(args: dict[str, Any]) -> str:
    path = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    if not path:
        return "[error] path required"
    if not _is_safe_write_path(path):
        return f"[error] запрещённый путь (системная директория): {path}"
    try:
        p = Path(path).expanduser().resolve()
        if not _is_safe_write_path(str(p)):
            return f"[error] запрещённый путь после resolve: {p}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[ok] записал {len(content)} символов в {p}"
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"


async def run_shell(args: dict[str, Any]) -> str:
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return "[error] command required"
    # Block obviously destructive patterns
    lowered = cmd.lower()
    blocked = ("format ", "del /s", "rm -rf", "rmdir /s", "shutdown", ":(){:|:&};:")
    for b in blocked:
        if b in lowered:
            return f"[error] заблокировано (опасная команда): {b}"
    try:
        # 30s timeout, capture stdout + stderr, run in PowerShell on Windows
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,  # noqa: S602 — opt-in by user via /admin/mcp
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            return "[error] команда превысила 30с — убита"
        result = []
        if out:
            result.append("STDOUT:\n" + out.decode("utf-8", errors="replace")[:4000])
        if err:
            result.append("STDERR:\n" + err.decode("utf-8", errors="replace")[:2000])
        result.append(f"exit_code: {proc.returncode}")
        return "\n\n".join(result)
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"


async def git_status(args: dict[str, Any]) -> str:
    repo = str(args.get("path", ".")).strip() or "."
    action = str(args.get("action", "status")).strip()  # status/log/diff
    allowed = {"status", "log", "diff", "branch"}
    if action not in allowed:
        return f"[error] action должен быть одним из {allowed}"
    try:
        p = Path(repo).expanduser().resolve()
        if not (p / ".git").exists():
            return f"[error] не git-репозиторий: {p}"
        if action == "log":
            cmd_args = ["git", "-C", str(p), "log", "--oneline", "-20"]
        elif action == "diff":
            cmd_args = ["git", "-C", str(p), "diff", "--stat"]
        elif action == "branch":
            cmd_args = ["git", "-C", str(p), "branch", "-v"]
        else:  # status
            cmd_args = ["git", "-C", str(p), "status", "-s", "-b"]
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        return out.decode("utf-8", errors="replace") or err.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"


# Tool registry — name → (function, description-for-LLM, params-schema)
_BUILTIN_TOOLS: dict[str, dict[str, Any]] = {
    "read_file": {
        "fn": read_file,
        "description": "Прочитать содержимое текстового файла на сервере.",
        "params": {"path": "путь к файлу (например 'D:\\Projects\\app.py')"},
    },
    "list_dir": {
        "fn": list_dir,
        "description": "Показать содержимое директории.",
        "params": {"path": "путь к директории"},
    },
    "write_file": {
        "fn": write_file,
        "description": "Создать или перезаписать файл.",
        "params": {
            "path": "путь к файлу",
            "content": "содержимое для записи",
        },
    },
    "run_shell": {
        "fn": run_shell,
        "description": "Выполнить shell/PowerShell команду. ОПАСНО.",
        "params": {"command": "shell-команда для выполнения"},
    },
    "git_status": {
        "fn": git_status,
        "description": "Прочитать состояние git-репозитория: status, log, diff, branch.",
        "params": {
            "path": "путь к репозиторию (директория с .git)",
            "action": "status | log | diff | branch",
        },
    },
}


def get_builtin_tool(name: str) -> dict[str, Any] | None:
    """Return registry entry for built-in tool, or None if unknown."""
    return _BUILTIN_TOOLS.get(name)


def list_builtin_tools() -> list[str]:
    return list(_BUILTIN_TOOLS.keys())


def builtin_command_to_tool_name(command: str) -> str | None:
    """Map ``builtin:read_file`` style command → tool name."""
    if not command.startswith("builtin:"):
        return None
    return command.removeprefix("builtin:")


async def call_tool(name: str, args: dict[str, Any]) -> str:
    """Dispatch a tool by name. Returns stringified result for the LLM."""
    entry = _BUILTIN_TOOLS.get(name)
    if entry is None:
        return f"[error] unknown tool: {name}"
    try:
        return await entry["fn"](args)
    except Exception as exc:
        log.exception("builtin_tool.failed", tool=name, args=args)
        return f"[error] tool crashed: {type(exc).__name__}: {exc}"


def build_tools_prompt(enabled_tool_names: list[str]) -> str:
    """Compose the system-prompt fragment describing available tools.

    The LLM emits a call as:
        <tool>name({"arg": "value"})</tool>
    Our parser picks this up between turns, executes, returns result,
    then asks the LLM to continue.
    """
    if not enabled_tool_names:
        return ""
    lines = [
        "",
        "У тебя есть инструменты. Чтобы вызвать — выведи строку:",
        "<tool>имя({\"параметр\": \"значение\"})</tool>",
        "Я выполню и пришлю результат в следующем сообщении. Можно вызывать несколько инструментов подряд.",
        "Доступные инструменты:",
    ]
    for name in enabled_tool_names:
        entry = _BUILTIN_TOOLS.get(name)
        if not entry:
            continue
        param_doc = ", ".join(
            f"{k}: {v}" for k, v in (entry.get("params") or {}).items()
        )
        lines.append(f"  • {name} — {entry['description']} ({param_doc})")
    return "\n".join(lines)


def parse_tool_calls(text: str) -> list[dict[str, Any]]:
    """Find ``<tool>name(args_json)</tool>`` patterns in LLM output.

    Returns list of ``{name, args}``. Forgiving parser: if args isn't
    valid JSON we still pick up the name with empty args, so a model
    that produces ``<tool>list_dir(D:\\Projects)</tool>`` still works.
    """
    import re  # noqa: PLC0415

    pattern = re.compile(
        r"<tool>\s*([a-zA-Z_][\w]*)\s*\((.*?)\)\s*</tool>", re.DOTALL
    )
    out: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        name = match.group(1).strip()
        raw_args = match.group(2).strip()
        args: dict[str, Any] = {}
        if raw_args:
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    args = {"value": parsed}
            except json.JSONDecodeError:
                # Try heuristic: single positional → path
                args = {"path": raw_args.strip("\"'")}
        out.append({"name": name, "args": args, "raw": match.group(0)})
    return out
