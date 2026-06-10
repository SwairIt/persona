"""T25 (2026-06-08) — Persona built-in tools (no Node.js / npm required).

T27 (2026-06-08) — tools now operate inside the per-user workspace
(``data/workspaces/{user_id}/``) instead of the whole server disk.
Relative paths like ``"app.py"`` resolve there; absolute paths outside
the workspace are refused with ``[error] path escapes workspace``.

Each tool has:
  * a stable ``name`` that the LLM sees in its system prompt
  * a JSON-schema-ish ``parameters`` blob (for the prompt)
  * an async ``run(args: dict, user_id: int) -> str`` function

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


async def read_file(args: dict[str, Any], user_id: int = 0) -> str:
    """T27 — resolves path inside the user's workspace."""
    from app.workspace import WorkspaceEscape, resolve_user_path  # noqa: PLC0415

    path = str(args.get("path", "")).strip()
    if not path:
        return "[error] path required"
    try:
        p = resolve_user_path(user_id, path)
    except WorkspaceEscape as exc:
        return f"[error] {exc}"
    try:
        if not p.exists():
            return f"[error] не существует: {p.name}"
        if p.is_dir():
            return f"[error] это директория, не файл: {p.name}"
        if p.stat().st_size > 500_000:
            return f"[error] файл больше 500 КБ ({p.stat().st_size} байт). Не открываю."
        content = p.read_text(encoding="utf-8", errors="replace")
        return f"[ok] {p.name}\n```\n{content}\n```"
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"


async def list_dir(args: dict[str, Any], user_id: int = 0) -> str:
    """T27 — defaults to the user's workspace root."""
    from app.workspace import (  # noqa: PLC0415
        WorkspaceEscape,
        ensure_user_workspace,
        resolve_user_path,
    )

    raw = str(args.get("path", "")).strip()
    try:
        if not raw or raw == ".":
            p = ensure_user_workspace(user_id)
        else:
            p = resolve_user_path(user_id, raw)
    except WorkspaceEscape as exc:
        return f"[error] {exc}"
    try:
        if not p.exists():
            return f"[error] не существует: {p.name}"
        if not p.is_dir():
            return f"[error] это файл, не директория: {p.name}"
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        rel_label = "workspace/" + (p.relative_to(ensure_user_workspace(user_id)).as_posix() if p != ensure_user_workspace(user_id) else "")
        lines = [f"[ok] {rel_label}", ""]
        for e in entries[:200]:
            tag = "DIR " if e.is_dir() else "FILE"
            size = "" if e.is_dir() else f" {e.stat().st_size}b"
            lines.append(f"  {tag} {e.name}{size}")
        if len(entries) > 200:
            lines.append(f"  … +{len(entries) - 200} ещё")
        if len(entries) == 0:
            lines.append("  (пусто)")
        return "\n".join(lines)
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"


async def write_file(args: dict[str, Any], user_id: int = 0) -> str:
    """T27 — writes into the user's workspace only. Absolute paths
    pointing outside are refused. Directories are created automatically.

    T28 — after a successful write, append a ``workspace_file_event`` row
    so the user's chosen code-write-target device can sync the file down.
    """
    from app.workspace import (  # noqa: PLC0415
        WorkspaceEscape,
        ensure_user_workspace,
        record_file_event,
        resolve_user_path,
    )

    path = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    if not path:
        return "[error] path required"
    try:
        p = resolve_user_path(user_id, path)
    except WorkspaceEscape as exc:
        return f"[error] {exc}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"

    # T28 — record for device sync. Best-effort: a logging failure must
    # not turn a successful write into an error for the user.
    try:
        rel = p.relative_to(ensure_user_workspace(user_id)).as_posix()
        await record_file_event(
            user_id, rel, "write", len(content.encode("utf-8"))
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("write_file.event_record_failed", path=path, error=str(exc))

    return f"[ok] записал {len(content)} символов в {p.name} (скачать: /workspace/file/{p.name})"


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


async def _resolve_ollama_endpoint() -> str:
    """The Ollama endpoint the user configured (for vision analysis).

    Primary source is the ``byo_api_key_ollama`` kv row — for Ollama the
    "API key" IS the endpoint URL, stored in plaintext (it's not a
    secret). make_client() wraps the client in a usage recorder that
    hides ``_endpoint``, so we read the kv directly.
    """
    try:
        from app.storage.db import get_connection  # noqa: PLC0415

        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT value FROM kv_settings WHERE key = 'byo_api_key_ollama'"
            )
            row = await cur.fetchone()
        if row and row[0]:
            return str(row[0]).strip().rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    # Fallback: dig through the make_client wrapper to its inner client.
    try:
        from app.llm.client import make_client  # noqa: PLC0415

        c = make_client()
        inner = getattr(c, "_inner", c)
        ep = getattr(inner, "_endpoint", "")
        if ep:
            return str(ep).rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return "http://localhost:11434"


async def _analyze_screenshot(png_path: Path, question: str) -> str:
    """Send a screenshot to a local Ollama vision model and return its
    description. Best-effort — failure returns a note, not an exception."""
    try:
        import base64 as _b64  # noqa: PLC0415

        from app.llm.client import CompletionRequest, OllamaClient  # noqa: PLC0415

        endpoint = await _resolve_ollama_endpoint()
        raw = png_path.read_bytes()
        data_url = "data:image/png;base64," + _b64.b64encode(raw).decode("ascii")
        client = OllamaClient(api_key=endpoint, model="qwen2.5vl:3b")
        out = await client.complete(
            CompletionRequest(
                system=(
                    "Ты анализируешь скриншот веб-страницы. Отвечай по-русски, "
                    "конкретно и по делу. Никаких иероглифов."
                ),
                user=question,
                image_data_url=data_url,
                max_tokens=1024,
                temperature=0.3,
            )
        )
        return out.strip() or "(vision-модель не дала описания)"
    except Exception as exc:  # noqa: BLE001
        return (
            f"(не смог проанализировать через vision-модель: {exc}). "
            "Скриншот сохранён — открой его по ссылке выше."
        )


async def web_browse(args: dict[str, Any], user_id: int = 0) -> str:
    """T29 — открыть URL в headless-браузере, сделать скриншот и
    проанализировать его vision-моделью. Браузер запускается в отдельном
    процессе, чтобы Chromium не трогал event loop сервера."""
    import sys as _sys  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from app.workspace import ensure_user_workspace  # noqa: PLC0415

    url = str(args.get("url", "")).strip()
    question = (
        str(args.get("question") or "").strip()
        or "Опиши подробно, что показано на этой веб-странице."
    )
    if not url:
        return "[error] нужен url"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    ws = ensure_user_workspace(user_id)
    bdir = ws / "browse"
    try:
        bdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"[error] не смог создать папку для скринов: {exc}"
    out = bdir / f"shot-{int(_time.time())}.png"

    repo_root = Path(__file__).resolve().parents[2]
    try:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable, "-m", "app.browse.shot", url, str(out),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(repo_root),
        )
        so, se = await asyncio.wait_for(proc.communicate(), timeout=75.0)
    except asyncio.TimeoutError:
        return "[error] браузер не успел загрузить страницу за 75с"
    except Exception as exc:  # noqa: BLE001
        return f"[error] не смог запустить браузер: {type(exc).__name__}: {exc}"

    line = so.decode("utf-8", "replace").strip() or se.decode("utf-8", "replace").strip()
    if not out.exists() or not line.startswith("OK"):
        return f"[error] браузер не справился: {line[:200] or 'нет вывода'}"
    title = line[2:].strip()
    rel = out.relative_to(ws).as_posix()
    analysis = await _analyze_screenshot(out, question)
    return (
        f"[ok] Открыл {url}\n"
        f"Заголовок страницы: {title}\n"
        f"Скриншот: /workspace/file/{rel}\n\n"
        f"Что на странице:\n{analysis}"
    )


async def install_skill(args: dict[str, Any], user_id: int = 0) -> str:
    """T29 — установить «навык» из GitHub-репозитория. Скачивает только
    текст-инструкции (SKILL.md/README.md), код не выполняется."""
    from app.skills.store import fetch_skill_from_github, save_skill  # noqa: PLC0415

    url = str(args.get("url", "")).strip()
    if not url:
        return "[error] нужна ссылка на GitHub-репозиторий"
    try:
        name, content, resolved = await fetch_skill_from_github(url)
    except ValueError as exc:
        return f"[error] {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"[error] не смог скачать навык: {type(exc).__name__}: {exc}"
    await save_skill(user_id, name, content, resolved)
    return (
        f"[ok] Установил навык «{name}» ({len(content)} символов) из {resolved}. "
        "Теперь я применяю его в этом и будущих чатах."
    )


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
    "web_browse": {
        "fn": web_browse,
        "description": (
            "Открыть веб-страницу в браузере, сделать скриншот и "
            "проанализировать его. Используй когда нужно посмотреть что-то "
            "в интернете и описать/проанализировать содержимое страницы."
        ),
        "params": {
            "url": "адрес страницы (https://...)",
            "question": "что именно посмотреть/проанализировать (опционально)",
        },
    },
    "install_skill": {
        "fn": install_skill,
        "description": (
            "Установить «навык» (skill) из GitHub: скачать инструкции "
            "(SKILL.md/README.md) по ссылке и начать им следовать. Вызывай "
            "когда пользователь говорит «установи скилл» и даёт ссылку."
        ),
        "params": {"url": "ссылка на GitHub-репозиторий со скиллом"},
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


async def call_tool(name: str, args: dict[str, Any], user_id: int = 0) -> str:
    """Dispatch a tool by name. Returns stringified result for the LLM.

    T27 — accepts user_id so workspace-aware tools resolve into the
    correct per-user directory. Tools that don't care (run_shell,
    git_status) ignore the parameter.
    """
    entry = _BUILTIN_TOOLS.get(name)
    if entry is None:
        return f"[error] unknown tool: {name}"
    try:
        fn = entry["fn"]
        # Inspect: workspace-aware tools accept user_id, legacy ones don't.
        import inspect  # noqa: PLC0415
        sig = inspect.signature(fn)
        if "user_id" in sig.parameters:
            return await fn(args, user_id=user_id)
        return await fn(args)
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
        "У тебя есть инструменты для работы с файлами. Все пути относительны "
        "ЛИЧНОГО WORKSPACE пользователя (data/workspaces/{user_id}/). Не нужно "
        "указывать абсолютные пути типа D:\\Projects — просто используй "
        "относительные: 'app.py', 'src/main.py', 'notes/idea.md'.",
        "",
        "Чтобы вызвать инструмент — выведи строку:",
        "<tool>имя({\"параметр\": \"значение\"})</tool>",
        "Я выполню и пришлю результат в следующем сообщении. Можно вызывать "
        "несколько инструментов подряд.",
        "",
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
