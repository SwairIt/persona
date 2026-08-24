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


async def _remote_fs(op: str, path: str, user_id: int, content: str | None = None) -> str | None:
    """T29 — when Mac-mode is on, run file ops on the Mac via the agent
    instead of the server workspace. Returns the result string, or None to
    fall through to the local workspace."""
    try:
        from app.devices.fs_rpc import is_enabled, run_remote  # noqa: PLC0415

        if not await is_enabled():
            return None
        return await run_remote(user_id, op, path, content)
    except Exception as exc:  # noqa: BLE001
        return f"[error] remote-fs: {type(exc).__name__}: {exc}"


async def read_file(args: dict[str, Any], user_id: int = 0) -> str:
    """T27 — resolves path inside the user's workspace (or the Mac in mac-mode)."""
    from app.workspace import WorkspaceEscape, resolve_user_path  # noqa: PLC0415

    path = _pick(args, ("path", "filepath", "file", "filename", "name")).strip()
    if not path:
        return "[error] path required"
    remote = await _remote_fs("read", path, user_id)
    if remote is not None:
        return remote
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
    remote = await _remote_fs("list", raw or ".", user_id)
    if remote is not None:
        return remote
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
        ws = ensure_user_workspace(user_id)
        rel = p.relative_to(ws).as_posix() if p != ws else ""
        prefix = (rel + "/") if rel else ""
        # Show each entry's FULL path relative to the workspace root so the
        # model can copy it verbatim into read_file/list_dir. Do NOT prepend
        # 'workspace/' — that's not part of the path.
        header = (
            f"[ok] содержимое: {rel or '(корень workspace)'}. "
            "Пути ниже передавай в инструменты РОВНО как написано, "
            "без префикса 'workspace/'."
        )
        lines = [header, ""]
        for e in entries[:200]:
            tag = "DIR " if e.is_dir() else "FILE"
            size = "" if e.is_dir() else f" {e.stat().st_size}b"
            lines.append(f"  {tag} {prefix}{e.name}{size}")
        if len(entries) > 200:
            lines.append(f"  … +{len(entries) - 200} ещё")
        if len(entries) == 0:
            lines.append("  (пусто)")
        return "\n".join(lines)
    except Exception as exc:
        return f"[error] {type(exc).__name__}: {exc}"


def _pick(args: dict[str, Any], keys: tuple[str, ...], default: str = "") -> str:
    """Взять первое присутствующее значение из возможных имён аргумента —
    слабые модели зовут параметры по-разному (path/filepath/file/name…)."""
    for k in keys:
        v = args.get(k)
        if v is not None:
            return str(v)
    return default


# Безрасширенные имена, которые ВСЁ-ТАКИ файлы (а не папки).
_KNOWN_EXTLESS_FILES = {
    "makefile", "dockerfile", "license", "readme", "procfile", "gemfile",
    "rakefile", "caddyfile", ".gitignore", ".gitkeep", ".env", ".dockerignore",
    ".npmrc", ".editorconfig", ".prettierrc",
}


def _looks_like_dir(path: str) -> bool:
    """Похоже ли, что path — это ПАПКА (нет расширения и это не известный
    безрасширенный файл), чтобы не плодить файлы вместо папок."""
    if path.rstrip().endswith(("/", "\\")):
        return True
    seg = path.rstrip("/\\").replace("\\", "/").split("/")[-1]
    if not seg:
        return False
    if seg.lower() in _KNOWN_EXTLESS_FILES:
        return False
    return "." not in seg


async def _make_dir(path: str, user_id: int = 0) -> str:
    """Создать папку (на Mac через агента или в workspace)."""
    path = path.strip().rstrip("/\\")
    if not path:
        return "[error] нужен path для папки"
    remote = await _remote_fs("write", f"{path}/.gitkeep", user_id, "")
    if remote is not None:
        return remote if str(remote).startswith("[error]") else f"[ok] создал папку {path}"
    from app.workspace import WorkspaceEscape, resolve_user_path  # noqa: PLC0415

    try:
        p = resolve_user_path(user_id, path)
    except WorkspaceEscape as exc:
        return f"[error] {exc}"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return f"[ok] создал папку {path}"
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

    path = _pick(args, ("path", "filepath", "file", "filename", "name", "dir")).strip()
    content = _pick(args, ("content", "text", "data", "body", "code", "value"))
    if not path:
        return "[error] нужен path (имя файла)"
    # Эвристика: пустой контент + путь без расширения = это ПАПКА, а не файл.
    # Слабые модели зовут write_file вместо mkdir — создаём папку как надо.
    if not content.strip() and _looks_like_dir(path):
        return await _make_dir(path, user_id)
    remote = await _remote_fs("write", path, user_id, content)
    if remote is not None:
        return remote
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


_DELETE_BAD = {"", ".", "..", "~", "/", "*", "."}


async def delete_path(args: dict[str, Any], user_id: int = 0) -> str:
    """Удалить файл ИЛИ папку (рекурсивно) на Mac пользователя (через агента,
    op=delete) либо в локальном workspace. С защитой от удаления корня/'..'."""
    path = _pick(args, ("path", "dir", "name", "file", "filepath", "filename", "folder")).strip()
    if not path:
        return "[error] нужен path: что удалить"
    parts = [seg for seg in path.replace("\\", "/").split("/")]
    if path.strip().strip("/") in _DELETE_BAD or path in ("/", "~", ".", "..") or any(
        seg == ".." for seg in parts
    ):
        return "[error] небезопасный путь для удаления (корень/.. запрещены)"

    from app.devices.fs_rpc import is_enabled, run_remote  # noqa: PLC0415

    if await is_enabled():
        return await run_remote(user_id, "delete", path)

    # локальный workspace (когда mac-fs выключен)
    import shutil  # noqa: PLC0415

    from app.workspace import WorkspaceEscape, resolve_user_path  # noqa: PLC0415

    try:
        p = resolve_user_path(user_id, path)
    except WorkspaceEscape as exc:
        return f"[error] {exc}"
    try:
        if not p.exists():
            return f"[error] не существует: {path}"
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"[ok] удалено: {path}"
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


async def _resolve_ollama_endpoint() -> str:
    """The Ollama endpoint the user configured (for vision analysis).

    Primary source is the ``byo_api_key_ollama`` kv row — for Ollama the
    "API key" IS the endpoint URL, stored in plaintext (it's not a
    secret). make_client() wraps the client in a usage recorder that
    hides ``_endpoint``, so we read the kv directly.

    ВНИМАНИЕ (per-user LLM): здесь СОЗНАТЕЛЬНО читается ГЛОБАЛЬНЫЙ kv, то
    есть Ollama владельца. Это безопасно ровно потому, что инструменты —
    зона только владельца, и это проверяется на ОБОИХ входах в чат:
    ``_tools_on = tools_owner and _private_tool_intent`` в
    ``app/web/routes/chat_sessions.py`` и ``tools_allowed = allow_tools and
    owner_actor`` в ``_stream_via_conversation_service`` (у не-владельца
    список инструментов пуст, а ``tool_policy`` = None). Если инструменты
    когда-нибудь откроют обычным пользователям — эту функцию НЕЛЬЗЯ
    оставлять как есть: она должна принимать ``user_id`` и читать
    ``user_settings``, иначе чужой чат пойдёт на железо владельца.
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

    # Run Chromium via a BLOCKING subprocess.run inside a worker thread.
    # asyncio.create_subprocess_exec raises NotImplementedError on Windows
    # under uvicorn's SelectorEventLoop — subprocess.run in a thread sidesteps
    # the event loop entirely and works regardless of loop type.
    def _shot() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [_sys.executable, "-m", "app.browse.shot", url, str(out)],
            capture_output=True,
            text=True,
            timeout=75,
            cwd=str(repo_root),
        )

    try:
        proc = await asyncio.to_thread(_shot)
    except subprocess.TimeoutExpired:
        return "[error] браузер не успел загрузить страницу за 75с"
    except Exception as exc:  # noqa: BLE001
        return f"[error] не смог запустить браузер: {type(exc).__name__}: {exc}"

    line = (proc.stdout or "").strip() or (proc.stderr or "").strip()
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
    mirror = ""
    try:
        from app.devices.fs_rpc import is_enabled  # noqa: PLC0415
        from app.skills.store import get_skills_dir  # noqa: PLC0415

        if await is_enabled():
            mirror = f" Также зеркалю файлом в папку {await get_skills_dir()} на устройстве."
    except Exception:  # noqa: BLE001, S110
        pass
    return (
        f"[ok] Установил навык «{name}» ({len(content)} символов) из {resolved}. "
        f"Теперь я применяю его в этом и будущих чатах.{mirror}"
    )


async def install_mcp(args: dict[str, Any], user_id: int = 0) -> str:
    """T31 — добавить и включить MCP-сервер по просьбе пользователя.
    Только регистрирует конфиг в БД (mcp_server); ничего не запускает само."""
    from app.mcp.servers import upsert_server  # noqa: PLC0415

    name = str(args.get("name", "")).strip()
    command = str(args.get("command", "") or args.get("url", "")).strip()
    description = str(args.get("description", "")).strip() or None
    if not name or not command:
        return "[error] нужны 'name' и 'command' (или 'url') MCP-сервера"
    try:
        sid = await upsert_server(
            name=name, description=description, command=command, enabled=True
        )
    except Exception as exc:  # noqa: BLE001
        return f"[error] не смог добавить MCP: {type(exc).__name__}: {exc}"
    return (
        f"[ok] MCP-сервер «{name}» добавлен и включён (id={sid}). "
        f"Команда: {command}. Управление — на странице /admin/mcp."
    )


# T31 E4 — опасные паттерны, которые не выполняем на устройстве ни при каких
# условиях. Список покрывает и Mac/Linux (bash), и Windows (PowerShell/cmd):
# агент сам выбирает shell по ОС, поэтому блок-лист общий.
_MAC_BLOCKED = (
    # unix
    "rm -rf /", "rm -rf ~", "mkfs", ":(){:|:&};:", "shutdown", "reboot",
    "diskutil erase", "> /dev/", "dd if=", "sudo rm",
    # windows
    "format ", "del /s", "del /f /s", "rmdir /s", "rd /s",
    "remove-item -recurse", "remove-item -force -recurse", "format-volume",
    "stop-computer", "restart-computer", "cipher /w",
)
_MAC_SHELLS = ("bash", "zsh", "sh", "powershell", "pwsh", "cmd")


async def run_mac(args: dict[str, Any], user_id: int = 0) -> str:
    """T31 E4 — выполнить команду на устройстве пользователя через агента.

    Кроссплатформенно: на Mac/Linux агент исполняет команду в bash, на Windows
    — в PowerShell. Ставит команду в очередь (op=exec) для выбранного
    code-target устройства; агент выполняет её в разрешённой папке (allowlist)
    и возвращает stdout/stderr/код. Требует включённого mac-fs и онлайн-агента —
    иначе возвращает [error] (не падает).

    shell: если не указан, агент берёт нативный для своей ОС (bash на Mac,
    powershell на Windows). Передавай shell только когда это важно.
    """
    from app.devices.fs_rpc import is_enabled, run_remote  # noqa: PLC0415

    command = str(args.get("command", "")).strip()
    if not command:
        return "[error] нужна команда (command)"
    # Пустой shell → "auto": агент сам подберёт нативный shell под свою ОС.
    shell = str(args.get("shell", "")).strip().lower()
    if shell and shell not in _MAC_SHELLS:
        shell = "auto"
    if not shell:
        shell = "auto"
    lowered = command.lower()
    for b in _MAC_BLOCKED:
        if b in lowered:
            return f"[error] заблокировано (опасная команда): {b}"
    if not await is_enabled():
        return (
            "[error] mac-fs выключен — включи на /settings/mac-fs, чтобы я мог "
            "выполнять команды на твоём Mac"
        )
    # path несёт тип шелла, content — саму команду (agent_fs_command, op=exec).
    return await run_remote(user_id, "exec", shell, command)


# ============================================================================
# Phase 1.2 — расширенный каталог инструментов (точечная правка, поиск, сеть).
# Переиспользуют resolve_user_path / _remote_fs / write_file / _pick.
# ============================================================================

import re as _re  # noqa: E402


async def _read_raw(path: str, user_id: int) -> tuple[str | None, str | None]:
    """Прочитать СЫРОЙ текст файла (workspace или Mac). → (content, error)."""
    from app.devices.fs_rpc import is_enabled, run_remote  # noqa: PLC0415

    if await is_enabled():
        res = await run_remote(user_id, "read", path)
        if res.startswith("[error]"):
            return None, res
        m = _re.search(r"```\n(.*)\n```\s*$", res, _re.DOTALL)
        return (m.group(1) if m else res), None
    from app.workspace import WorkspaceEscape, resolve_user_path  # noqa: PLC0415

    try:
        p = resolve_user_path(user_id, path)
    except WorkspaceEscape as exc:
        return None, f"[error] {exc}"
    if not p.exists():
        return None, f"[error] не существует: {path}"
    if p.is_dir():
        return None, f"[error] это директория: {path}"
    try:
        return p.read_text(encoding="utf-8", errors="replace"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"[error] {type(exc).__name__}: {exc}"


async def edit_file(args: dict[str, Any], user_id: int = 0) -> str:
    """Точечная замена old→new в существующем файле (не перезаписывает целиком)."""
    path = _pick(args, ("path", "file", "filepath", "filename", "name")).strip()
    old = _pick(args, ("old", "old_string", "old_str", "find", "search", "from"))
    new = _pick(args, ("new", "new_string", "new_str", "replace", "to", "with"))
    if not path:
        return "[error] нужен path"
    if not old:
        return "[error] нужен old (точный фрагмент, который заменить)"
    content, err = await _read_raw(path, user_id)
    if err:
        return err
    n = content.count(old)
    if n == 0:
        return (
            f"[error] фрагмент не найден в {path}. Сначала прочитай файл (read_file) "
            "и скопируй точный текст с отступами."
        )
    count_raw = args.get("count")
    if n > 1 and not count_raw:
        return (
            f"[error] фрагмент встречается {n} раз — неоднозначно. Добавь контекст "
            "в old или передай count=N."
        )
    count = int(count_raw) if count_raw else 1
    updated = content.replace(old, new, count)
    res = await write_file({"path": path, "content": updated}, user_id=user_id)
    if str(res).startswith("[error]"):
        return res
    return f"[ok] изменён {path}: заменено {min(n, count)} вхожд. (было {len(content)}→{len(updated)} симв.)"


async def multi_edit(args: dict[str, Any], user_id: int = 0) -> str:
    """Несколько точечных замен в одном файле за один проход/запись.

    edits: [{"old": "...", "new": "..."}] — применяются по порядку.
    """
    path = _pick(args, ("path", "file", "filepath", "filename", "name")).strip()
    edits = args.get("edits") or args.get("changes") or []
    if not path:
        return "[error] нужен path"
    if not isinstance(edits, list) or not edits:
        return '[error] нужен edits: [{"old":..., "new":...}]'
    content, err = await _read_raw(path, user_id)
    if err:
        return err
    applied = 0
    for i, e in enumerate(edits):
        old = str(e.get("old", e.get("old_string", "")))
        new = str(e.get("new", e.get("new_string", "")))
        if not old:
            return f"[error] правка #{i + 1}: пустой old"
        if old not in content:
            return f"[error] правка #{i + 1}: фрагмент не найден — прочитай файл и уточни"
        content = content.replace(old, new, 1)
        applied += 1
    res = await write_file({"path": path, "content": content}, user_id=user_id)
    if str(res).startswith("[error]"):
        return res
    return f"[ok] {path}: применено {applied} правок"


async def read_many(args: dict[str, Any], user_id: int = 0) -> str:
    """Прочитать несколько файлов за один вызов (paths: [...]). Экономит раунды."""
    paths = args.get("paths") or args.get("files") or []
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.replace(",", "\n").splitlines() if p.strip()]
    if not paths:
        return '[error] нужен paths: ["a.py","b.py"]'
    out: list[str] = []
    total = 0
    for path in paths[:20]:
        content, err = await _read_raw(str(path), user_id)
        if err:
            out.append(f"### {path}\n{err}")
            continue
        if total + len(content) > 120_000:
            out.append(f"### {path}\n[обрезано: достигнут лимит суммарного объёма]")
            break
        total += len(content)
        out.append(f"### {path}\n```\n{content}\n```")
    return "\n\n".join(out)


async def find_files(args: dict[str, Any], user_id: int = 0) -> str:
    """Найти файлы по glob-маске в workspace (например '**/*.py')."""
    import fnmatch  # noqa: PLC2701, PLC0415

    from app.workspace import (  # noqa: PLC0415
        WorkspaceEscape,
        ensure_user_workspace,
        resolve_user_path,
    )

    glob = _pick(args, ("glob", "pattern", "mask", "query", "name")).strip() or "*"
    base = str(args.get("path", "")).strip()
    try:
        root = resolve_user_path(user_id, base) if base and base != "." else ensure_user_workspace(user_id)
        ws = ensure_user_workspace(user_id)
    except WorkspaceEscape as exc:
        return f"[error] {exc}"
    if not root.exists():
        return f"[error] нет такой папки: {base}"
    matches: list[str] = []
    pat = glob if ("/" in glob or "*" in glob) else f"**/*{glob}*"
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(ws).as_posix()
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, glob):
                matches.append(rel)
        if len(matches) >= 300:
            break
    if not matches:
        return f"[ok] ничего не найдено по '{glob}'"
    return f"[ok] найдено {len(matches)}:\n" + "\n".join(sorted(matches))


async def search_code(args: dict[str, Any], user_id: int = 0) -> str:
    """Поиск текста/regex по файлам workspace (grep). query[, glob][, regex]."""
    import fnmatch  # noqa: PLC2701, PLC0415

    from app.workspace import ensure_user_workspace  # noqa: PLC0415

    query = _pick(args, ("query", "q", "pattern", "text", "search")).strip()
    if not query:
        return "[error] нужен query"
    glob = str(args.get("glob", "")).strip()
    use_regex = bool(args.get("regex"))
    ws = ensure_user_workspace(user_id)
    try:
        rx = _re.compile(query if use_regex else _re.escape(query), _re.IGNORECASE)
    except _re.error as exc:
        return f"[error] плохой regex: {exc}"
    hits: list[str] = []
    scanned = 0
    for p in ws.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ws).as_posix()
        if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(p.name, glob)):
            continue
        if p.stat().st_size > 1_000_000:
            continue
        scanned += 1
        if scanned > 2000:
            break
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 100:
                        break
        except Exception:  # noqa: BLE001, S112
            continue
        if len(hits) >= 100:
            break
    if not hits:
        return f"[ok] совпадений нет: '{query}'"
    return f"[ok] {len(hits)} совпадений:\n" + "\n".join(hits)


def _url_is_safe(url: str) -> tuple[bool, str]:
    """Allowlist http(s) + запрет localhost/частных сетей (SSRF-защита)."""
    import ipaddress  # noqa: PLC0415
    import socket  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415

    try:
        u = urlparse(url)
    except Exception:  # noqa: BLE001
        return False, "плохой URL"
    if u.scheme not in ("http", "https"):
        return False, "только http/https"
    host = u.hostname or ""
    if not host:
        return False, "нет хоста"
    if host.lower() in ("localhost", "0.0.0.0", "::1"):  # noqa: S104
        return False, "localhost запрещён"
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False, "частная сеть запрещена"
    except Exception:  # noqa: BLE001 — DNS fail → let httpx report it
        pass
    return True, ""


async def fetch_json(args: dict[str, Any], user_id: int = 0) -> str:
    """HTTP-запрос (GET/POST). url[, method][, headers][, body]. До 1 МБ / 20с."""
    import httpx  # noqa: PLC0415

    url = _pick(args, ("url", "endpoint", "uri")).strip()
    if not url:
        return "[error] нужен url"
    ok, why = _url_is_safe(url)
    if not ok:
        return f"[error] {why}"
    method = (str(args.get("method", "GET")) or "GET").upper()
    headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
    body = args.get("body") or args.get("json") or args.get("data")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as cli:
            kw: dict[str, Any] = {"headers": headers}
            if body is not None and method in ("POST", "PUT", "PATCH"):
                if isinstance(body, (dict, list)):
                    kw["json"] = body
                else:
                    kw["content"] = str(body)
            resp = await cli.request(method, url, **kw)
            text = resp.text[:1_000_000]
        return f"[ok] {method} {url} → {resp.status_code}\n{text}"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {type(exc).__name__}: {exc}"


async def _brave_key() -> str:
    """Brave API key from env, else ``kv_settings`` (set from /settings/web-search)."""
    from app.storage.db import get_connection  # noqa: PLC0415

    key = (os.getenv("BRAVE_API_KEY") or os.getenv("PERSONA_BRAVE_API_KEY") or "").strip()
    if key:
        return key
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT value FROM kv_settings WHERE key IN ('byo_api_key_brave','brave_api_key') LIMIT 1"
            )
            row = await cur.fetchone()
            if row:
                return str(row[0]).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _search_brave(query: str, n: int, key: str, images: bool = False) -> str:
    """Brave Search API — preferred provider when a key is configured."""
    import httpx  # noqa: PLC0415

    url = (
        "https://api.search.brave.com/res/v1/images/search"
        if images
        else "https://api.search.brave.com/res/v1/web/search"
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as cli:
            resp = await cli.get(
                url,
                params={"q": query, "count": min(n, 10)},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
            )
            data = resp.json()
        if images:
            results = data.get("results") or []
            if not results:
                return f"[ok] ничего не найдено: {query}"
            out = [f"[ok] поиск «{query}»:"]
            for r in results[:n]:
                media = (r.get("properties") or {}).get("url") or r.get("url", "")
                out.append(f"- {r.get('title', '')}\n  {media}")
            return "\n".join(out)
        results = (data.get("web") or {}).get("results") or []
        if not results:
            return f"[ok] ничего не найдено: {query}"
        out = [f"[ok] поиск «{query}»:"]
        for r in results[:n]:
            out.append(f"- {r.get('title', '')}\n  {r.get('url', '')}\n  {r.get('description', '')[:200]}")
        return "\n".join(out)
    except Exception as exc:  # noqa: BLE001 — network error or malformed JSON, never raise
        return f"[error] поиск не удался: {type(exc).__name__}: {exc}"


def _ddg_extract_url(href: str) -> str:
    """DuckDuckGo HTML results wrap links in a ``/l/?uddg=<encoded>`` redirect."""
    from urllib.parse import parse_qs, unquote, urlparse  # noqa: PLC0415

    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    if qs.get("uddg"):
        return unquote(qs["uddg"][0])
    return href


def _parse_duckduckgo_html(html_text: str, n: int) -> list[dict[str, str]]:
    """Extract title/url/snippet triples from a DuckDuckGo HTML results page."""
    import html as html_mod  # noqa: PLC0415
    import re  # noqa: PLC0415

    pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</a>',
        re.S,
    )
    results: list[dict[str, str]] = []
    for href, title_html, snippet_html in pattern.findall(html_text):
        title = html_mod.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippet_html)).strip()
        url = _ddg_extract_url(href)
        if not title or not url:
            continue
        results.append({"title": title, "url": url, "description": snippet})
        if len(results) >= n:
            break
    return results


async def _search_duckduckgo(query: str, n: int) -> str:
    """Keyless web-search fallback — DuckDuckGo's HTML endpoint, no API key."""
    import httpx  # noqa: PLC0415

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cli:
            resp = await cli.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (compatible; PersonaBot/1.0)"},
            )
            html_text = resp.text
        results = _parse_duckduckgo_html(html_text, n)
    except Exception as exc:  # noqa: BLE001 — network error or malformed HTML, never raise
        return f"[error] поиск не удался: {type(exc).__name__}: {exc}"
    if not results:
        return f"[ok] ничего не найдено: {query}"
    out = [f"[ok] поиск «{query}»:"]
    for r in results:
        out.append(f"- {r['title']}\n  {r['url']}\n  {r['description'][:200]}")
    return "\n".join(out)


async def _wikipedia_search_one(lang: str, query: str, n: int) -> list[dict[str, str]]:
    """One language edition's search hits, via Wikipedia's keyless public API."""
    import html as html_mod  # noqa: PLC0415
    import re  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    url = f"https://{lang}.wikipedia.org/w/api.php"
    ok, why = _url_is_safe(url)
    if not ok:
        return []
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cli:
        resp = await cli.get(
            url,
            params={
                "action": "query",
                "list": "search",
                "format": "json",
                "srsearch": query,
                "srlimit": min(n, 10),
            },
            headers={"User-Agent": "Mozilla/5.0 (compatible; PersonaBot/1.0)"},
        )
        data = resp.json()
    hits = ((data.get("query") or {}).get("search")) or []
    results: list[dict[str, str]] = []
    for hit in hits[:n]:
        title = str(hit.get("title", "")).strip()
        if not title:
            continue
        snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", str(hit.get("snippet", "")))).strip()
        page_url = f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}"
        results.append({"title": title, "url": page_url, "description": snippet})
    return results


async def _search_wikipedia(query: str, n: int) -> str:
    """Keyless provider tried before DuckDuckGo — Wikipedia's public search API
    needs no key and does not CAPTCHA (unlike DuckDuckGo's HTML endpoint).
    Russian Wikipedia first; if that search is empty, fall back to English
    (a topic may only have an English article, or the ru search may miss it)."""
    try:
        results = await _wikipedia_search_one("ru", query, n)
        if not results:
            results = await _wikipedia_search_one("en", query, n)
    except Exception as exc:  # noqa: BLE001 — network error or malformed JSON, never raise
        return f"[error] поиск не удался: {type(exc).__name__}: {exc}"
    if not results:
        return f"[ok] ничего не найдено: {query}"
    out = [f"[ok] поиск «{query}»:"]
    for r in results:
        out.append(f"- {r['title']}\n  {r['url']}\n  {r['description'][:200]}")
    return "\n".join(out)


async def _search_openverse(query: str, n: int) -> str:
    """Keyless image/GIF fallback — Openverse's public API (CC-licensed media, no key)."""
    import httpx  # noqa: PLC0415

    params: dict[str, Any] = {"q": query, "page_size": min(n, 10)}
    if "gif" in query.lower():
        params["extension"] = "gif"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cli:
            resp = await cli.get(
                "https://api.openverse.org/v1/images/",
                params=params,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PersonaBot/1.0)"},
            )
            data = resp.json()
        results = data.get("results") or []
    except Exception as exc:  # noqa: BLE001 — network error or malformed JSON, never raise
        return f"[error] поиск не удался: {type(exc).__name__}: {exc}"
    if not results:
        return f"[ok] ничего не найдено: {query}"
    out = [f"[ok] поиск «{query}»:"]
    for r in results[:n]:
        out.append(f"- {r.get('title', '')}\n  {r.get('url', '')}")
    return "\n".join(out)


_IMAGE_KINDS = frozenset({"images", "image", "gif", "gifs"})


async def web_search(args: dict[str, Any], user_id: int = 0) -> str:
    """Поиск в интернете. Brave (если есть ключ), иначе — без ключа (Wikipedia /
    DuckDuckGo / Openverse). query[, n][, kind: web|images]. Возвращает
    title/url/snippet (или прямые ссылки на медиа при kind=images)."""
    query = _pick(args, ("query", "q", "text", "search")).strip()
    if not query:
        return "[error] нужен query"
    n = int(args.get("n", 5) or 5)
    kind = str(args.get("kind", "web") or "web").strip().lower()
    is_images = kind in _IMAGE_KINDS

    key = await _brave_key()
    if key:
        result = await _search_brave(query, n, key, images=is_images)
        if not result.startswith("[error]"):
            return result
        log.warning("web_search.brave_failed_fallback", detail=result[:200])
    # Keyless fallback chain — either no key configured, or Brave just
    # errored. Wikipedia first (no CAPTCHA, great for exactly the kind of
    # topic — films, books, people, events — someone asks Persona to look
    # up), then DuckDuckGo's HTML endpoint as the last resort.
    if is_images:
        return await _search_openverse(query, n)
    result = await _search_wikipedia(query, n)
    if not result.startswith("[error]") and not result.startswith("[ok] ничего не найдено"):
        return result
    return await _search_duckduckgo(query, n)


async def verify_media_url(args: dict[str, Any], user_id: int = 0) -> str:
    """HEAD-проверка, что url реально отдаёт image/*, прежде чем утверждать
    'отправил картинку/gif'. url."""
    import httpx  # noqa: PLC0415

    url = _pick(args, ("url", "media_url", "image_url")).strip()
    if not url:
        return "[error] нужен url"
    ok, why = _url_is_safe(url)
    if not ok:
        return f"[error] {why}"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as cli:
            resp = await cli.head(url)
            ctype = resp.headers.get("content-type", "")
            if resp.status_code >= 400 or not ctype.lower().startswith("image/"):
                # Some hosts reject HEAD (405) or omit content-type on it —
                # fall back to a ranged GET so we don't false-negative.
                resp = await cli.get(url, headers={"Range": "bytes=0-0"})
                ctype = resp.headers.get("content-type", "")
    except Exception as exc:  # noqa: BLE001
        return f"[error] проверка не удалась: {type(exc).__name__}: {exc}"
    if ctype.lower().startswith("image/"):
        return f"[ok] это изображение ({ctype}): {url}"
    return f"[error] не изображение (content-type={ctype or 'unknown'}): {url}"


async def run_tests(args: dict[str, Any], user_id: int = 0) -> str:
    """Запустить тесты на устройстве (pytest/npm test через run_mac) или на сервере."""
    cmd = _pick(args, ("cmd", "command")).strip()
    path = _pick(args, ("path", "dir")).strip()
    if not cmd:
        cmd = "pytest -q" + (f" {path}" if path else "")
    from app.devices.fs_rpc import is_enabled  # noqa: PLC0415

    if await is_enabled():
        return await run_mac({"command": cmd, "shell": "auto"}, user_id=user_id)
    return await run_shell({"command": cmd})


async def query_memory(args: dict[str, Any], user_id: int = 0) -> str:
    """Осознанно вспомнить: личные факты (user_memory) + релевантное из чатов."""
    query = _pick(args, ("query", "q", "text", "about")).strip()
    if not query:
        return "[error] нужен query"
    parts: list[str] = []
    try:
        from app.chat.user_memory import search_memory  # noqa: PLC0415

        facts = await search_memory(user_id, query, limit=8)
        if facts:
            parts.append("Факты о пользователе:\n" + "\n".join(f"• {f['text']}" for f in facts))
    except Exception as exc:  # noqa: BLE001
        log.debug("query_memory.facts_failed", error=str(exc))
    try:
        from app.chat.sessions import recall_relevant  # noqa: PLC0415

        block = await recall_relevant(user_id, query, exclude_session_id=None)
        if block:
            parts.append("Из прошлых чатов:\n" + block)
    except Exception as exc:  # noqa: BLE001
        log.debug("query_memory.recall_failed", error=str(exc))
    return "[ok] из памяти:\n" + "\n\n".join(parts) if parts else "[ok] ничего релевантного не нашёл"


async def schedule_reminder(args: dict[str, Any], user_id: int = 0) -> str:
    """Создать напоминание из естественного языка («напомни завтра …»).

    Принимает либо ``text`` (фраза целиком — распарсим дату), либо явные
    ``body`` + ``when`` (when: 'сегодня'/'завтра'/'через 3 дня'/'20.06'/...).
    Пишет в таблицу reminders. Возвращает человекочитаемое подтверждение.
    """
    from datetime import date  # noqa: PLC0415

    from app.chat.reminder_nl import parse_reminder  # noqa: PLC0415
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.reminders import create_reminder  # noqa: PLC0415

    text = _pick(args, ("text", "phrase", "query", "input")).strip()
    body_in = _pick(args, ("body", "task", "title", "what", "message")).strip()
    when_in = _pick(args, ("when", "date", "due", "due_date")).strip()

    if body_in:
        # явные поля: дату берём из when (если задан), тело — как есть
        parsed = parse_reminder(f"{when_in} {body_in}".strip())
        body = body_in
        due_iso = parsed["due_date"]
    elif text:
        parsed = parse_reminder(text)
        body = parsed["body"]
        due_iso = parsed["due_date"]
    else:
        return "[error] нужен text (фраза) или body+when"

    if not body:
        return "[error] не понял, о чём напомнить"

    try:
        due = date.fromisoformat(due_iso)
        async with get_connection() as conn:
            rid = await create_reminder(conn, body=body, due_date=due)
    except Exception as exc:  # noqa: BLE001
        log.warning("schedule_reminder.failed", error=str(exc))
        return f"[error] не удалось сохранить напоминание: {exc}"

    return (
        f"[ok] напоминание #{rid} на {due_iso}: «{body}». "
        "Покажется в списке дел на эту дату (/reminders)."
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
    "install_mcp": {
        "fn": install_mcp,
        "description": (
            "Добавить и включить MCP-сервер по просьбе пользователя «установи "
            "mcp …». Укажи name (короткое имя) и command (команда запуска, "
            "напр. 'npx -y @modelcontextprotocol/server-filesystem ~/Projects' "
            "или URL). Сервер появится включённым в /admin/mcp."
        ),
        "params": {
            "name": "короткое имя сервера",
            "command": "команда запуска MCP-сервера или URL",
            "description": "что делает (опционально)",
        },
    },
    "delete_path": {
        "fn": delete_path,
        "description": (
            "Удалить файл ИЛИ папку (рекурсивно) на Mac. Используй, когда просят "
            "«удали …». Папки удаляются вместе с содержимым."
        ),
        "params": {"path": "путь к файлу или папке (относительно рабочей папки)"},
    },
    "run_mac": {
        "fn": run_mac,
        "description": (
            "Выполнить команду НА УСТРОЙСТВЕ пользователя через агента, в "
            "разрешённой папке. Кроссплатформенно: на Mac/Linux — bash, на "
            "Windows — PowerShell (агент сам выберёт по своей ОС). Используй "
            "для реальных действий: запустить скрипт, собрать проект, git, "
            "установить пакет. Возвращает stdout/stderr/код. Требует "
            "включённого mac-fs и онлайн-агента."
        ),
        "params": {
            "command": "команда для выполнения на устройстве",
            "shell": (
                "необязательно: bash | zsh | sh | powershell | pwsh | cmd. "
                "Без него агент берёт нативный shell своей ОС."
            ),
        },
    },
    # --- Phase 1.2: расширенный каталог ---
    "edit_file": {
        "fn": edit_file,
        "description": (
            "Точечно изменить существующий файл: заменить ТОЧНЫЙ фрагмент old на new "
            "(не перезаписывает весь файл). Сначала прочитай файл (read_file) и скопируй "
            "точный текст. Если фрагмент встречается несколько раз — уточни контекст или count."
        ),
        "params": {"path": "путь к файлу", "old": "точный фрагмент (что заменить)",
                   "new": "на что заменить", "count": "необяз.: сколько вхождений"},
    },
    "multi_edit": {
        "fn": multi_edit,
        "description": "Несколько точечных замен в одном файле за раз (edits: [{old,new}]).",
        "params": {"path": "путь к файлу", "edits": '[{"old":"...","new":"..."}]'},
    },
    "read_many": {
        "fn": read_many,
        "description": "Прочитать несколько файлов сразу (экономит раунды).",
        "params": {"paths": '["a.py","b.py"]'},
    },
    "find_files": {
        "fn": find_files,
        "description": "Найти файлы по glob-маске в workspace ('**/*.py', 'main', и т.п.).",
        "params": {"glob": "маска или подстрока имени", "path": "необяз.: подпапка"},
    },
    "search_code": {
        "fn": search_code,
        "description": "Поиск текста/regex по файлам workspace (grep). Вернёт file:line.",
        "params": {"query": "что искать", "glob": "необяз.: маска файлов",
                   "regex": "необяз.: true для regex"},
    },
    "fetch_json": {
        "fn": fetch_json,
        "description": "HTTP-запрос к API (GET/POST). Только http(s), без локальной сети. До 1 МБ.",
        "params": {"url": "адрес", "method": "GET|POST|...", "headers": "{}", "body": "тело/JSON"},
    },
    "web_search": {
        "fn": web_search,
        "description": (
            "Поиск в интернете. Brave, если есть ключ (/settings/web-search), "
            "иначе — без ключа (Wikipedia/DuckDuckGo/Openverse), поиск всегда работает. "
            "Вернёт title/url/описание, при kind=images — прямые ссылки на медиа."
        ),
        "params": {"query": "запрос", "n": "сколько результатов",
                   "kind": "необяз.: web (деф.) | images — для картинок/gif"},
    },
    "verify_media_url": {
        "fn": verify_media_url,
        "description": (
            "HEAD-проверка, что url реально отдаёт картинку/gif (content-type image/*). "
            "Вызывай ПЕРЕД тем, как утверждать 'я отправила картинку' — если проверка "
            "не прошла, не притворяйся, что отправила."
        ),
        "params": {"url": "адрес медиа"},
    },
    "run_tests": {
        "fn": run_tests,
        "description": "Запустить тесты (pytest/npm) на устройстве (через mac-fs) или на сервере.",
        "params": {"cmd": "необяз.: команда (деф. pytest -q)", "path": "необяз.: путь"},
    },
    "query_memory": {
        "fn": query_memory,
        "description": "Осознанно вспомнить релевантное из всех прошлых чатов пользователя.",
        "params": {"query": "о чём вспомнить"},
    },
    "schedule_reminder": {
        "fn": schedule_reminder,
        "description": (
            "Создать напоминание/задачу из естественного языка. Вызывай, когда "
            "пользователь просит «напомни …», «через час/завтра/в пятницу …», "
            "«поставь задачу …». Передай фразу целиком в text — дату распознаю сам."
        ),
        "params": {
            "text": "фраза целиком, напр. 'напомни завтра оплатить хостинг'",
            "body": "необяз.: только суть задачи (если дата отдельно во when)",
            "when": "необяз.: дата словами — сегодня/завтра/через 3 дня/20.06",
        },
    },
}


# Phase 2 — merge the interactive browser-agent tools (persistent Playwright
# worker per session) into the registry. Imported here, not at module top, to
# keep the import graph shallow (the agent package pulls in workspace + SSE
# lazily). A failure to import must not take down the whole tool registry.
try:
    from app.browse.agent import BROWSER_AGENT_ALIASES, BROWSER_AGENT_TOOLS  # noqa: E402

    _BUILTIN_TOOLS.update(BROWSER_AGENT_TOOLS)
except Exception as _exc:  # noqa: BLE001
    log.warning("builtin_tools.browser_agent_import_failed", error=str(_exc))
    BROWSER_AGENT_ALIASES = {}


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


# T29 — weak models invent tool names (write_dir, create_file, ls...). Map
# the common hallucinations to real tools so file creation just works.
_TOOL_ALIASES: dict[str, str] = {
    "write_dir": "write_file", "create_file": "write_file", "createfile": "write_file",
    "new_file": "write_file", "make_file": "write_file", "save_file": "write_file",
    "writefile": "write_file", "create": "write_file",
    "read": "read_file", "readfile": "read_file", "open_file": "read_file", "cat": "read_file",
    "ls": "list_dir", "dir": "list_dir", "listdir": "list_dir", "list_files": "list_dir",
    "browse": "web_browse", "open_url": "web_browse", "fetch_url": "web_browse",
    "touch": "write_file", "createfile": "write_file", "appendfile": "write_file",
    "put_file": "write_file", "savefile": "write_file",
    # удаление: модели выдумывают разные имена → ведём на delete_path
    "rm": "delete_path", "rm_dir": "delete_path", "rmdir": "delete_path",
    "remove": "delete_path", "delete": "delete_path", "delete_file": "delete_path",
    "delete_dir": "delete_path", "del": "delete_path", "unlink": "delete_path",
    "remove_file": "delete_path", "remove_dir": "delete_path",
    # выполнение команд на устройстве: run_mac теперь кроссплатформенный —
    # модели зовут его по-разному (особенно на Windows) → ведём всё на run_mac.
    "run_pc": "run_mac", "run_windows": "run_mac", "run_win": "run_mac",
    "run_device": "run_mac", "run_remote": "run_mac", "run_shell_remote": "run_mac",
    "run_powershell": "run_mac", "powershell": "run_mac", "run_command": "run_mac",
    "exec": "run_mac", "exec_command": "run_mac", "run_on_device": "run_mac",
    "shell_exec": "run_mac", "run_bash": "run_mac",
    # Phase 1.2 — новые инструменты: частые синонимы от моделей
    "patch_file": "edit_file", "replace_in_file": "edit_file", "str_replace": "edit_file",
    "apply_edit": "edit_file", "edit": "edit_file",
    "grep": "search_code", "search_files": "search_code", "find_in_files": "search_code",
    "glob": "find_files", "find": "find_files", "find_file": "find_files",
    "read_files": "read_many", "cat_many": "read_many",
    "fetch": "fetch_json", "http_request": "fetch_json", "http_get": "fetch_json",
    "curl": "fetch_json", "request": "fetch_json", "api_call": "fetch_json",
    "search": "web_search", "google": "web_search", "search_web": "web_search",
    "test": "run_tests", "pytest": "run_tests", "npm_test": "run_tests",
    "recall": "query_memory", "remember_search": "query_memory", "search_memory": "query_memory",
    # NL-планирование: модели зовут по-разному → ведём на schedule_reminder
    "remind": "schedule_reminder", "remind_me": "schedule_reminder",
    "add_reminder": "schedule_reminder", "create_reminder": "schedule_reminder",
    "set_reminder": "schedule_reminder", "schedule_task": "schedule_reminder",
    "add_task": "schedule_reminder", "create_task": "schedule_reminder",
    "reminder": "schedule_reminder", "remindme": "schedule_reminder",
}
_MKDIR_NAMES = {
    "mkdir", "make_dir", "makedir", "create_dir", "createdir", "create_directory",
    "folder", "create_folder", "createfolder", "make_folder", "new_dir", "newdir",
    "new_folder", "newfolder", "mkdirs",
}

# Phase 2 — fold the browser-agent hallucination aliases into the main map
# (only those whose real target isn't already a registered builtin name, so a
# real tool name is never shadowed).
for _alias, _target in (BROWSER_AGENT_ALIASES or {}).items():
    _TOOL_ALIASES.setdefault(_alias, _target)


async def call_tool(
    name: str, args: dict[str, Any], user_id: int = 0, session_id: int | None = None
) -> str:
    """Dispatch a tool by name. Returns stringified result for the LLM.

    T27 — accepts user_id so workspace-aware tools resolve into the
    correct per-user directory. Tools that don't care (run_shell,
    git_status) ignore the parameter.

    Phase 2 — accepts session_id so session-scoped tools (the persistent
    browser agent, MCP routing, activity logging) can bind to the right
    live session. Tools opt in by declaring a ``session_id`` parameter;
    the dispatcher only passes kwargs a tool actually accepts.
    """
    name = (name or "").strip()
    # Phase 2 — external MCP tools live under the ``mcp__server__tool``
    # namespace; route them to the stdio MCP runtime (no user/session
    # binding — the MCP server has its own scope/allowlist).
    if name.startswith("mcp__"):
        from app.mcp.runtime import call_mcp_tool  # noqa: PLC0415

        return await call_mcp_tool(name, args)
    # mkdir-style → create a folder by writing a .gitkeep inside it.
    if name in _MKDIR_NAMES:
        path = str(args.get("path") or args.get("dir") or args.get("name") or "").strip()
        return await _make_dir(path, user_id)
    name = _TOOL_ALIASES.get(name, name)
    entry = _BUILTIN_TOOLS.get(name)
    if entry is None:
        available = ", ".join(_BUILTIN_TOOLS.keys())
        return (
            f"[error] нет инструмента '{name}'. Доступные: {available}. "
            "Для файлов используй write_file({\"path\":..., \"content\":...})."
        )
    try:
        fn = entry["fn"]
        # Inspect: pass only the kwargs each tool declares (user_id / session_id).
        import inspect  # noqa: PLC0415
        params = inspect.signature(fn).parameters
        kwargs: dict[str, Any] = {}
        if "user_id" in params:
            kwargs["user_id"] = user_id
        if "session_id" in params:
            kwargs["session_id"] = session_id
        return await fn(args, **kwargs)
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
        "━━━━━ ТВОИ ИНСТРУМЕНТЫ (РАБОТАЮТ ПРЯМО СЕЙЧАС) ━━━━━",
        "У тебя ЕСТЬ реальные инструменты: открыть сайт в браузере, читать/писать "
        "файлы, установить навык и др. Они ВЫПОЛНЯЮТСЯ на сервере по-настоящему.",
        "",
        "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО отвечать «я не могу», «у меня нет доступа», «я "
        "всего лишь ИИ», «запусти скрипт сам», «вот как сделать это вручную». Если "
        "задача решается инструментом — ты ОБЯЗАН вызвать инструмент, а НЕ "
        "отказываться и НЕ объяснять ручной способ.",
        "",
        "Как вызвать — выведи ОТДЕЛЬНОЙ строкой ровно такой синтаксис:",
        "<tool>имя({\"параметр\": \"значение\"})</tool>",
        "Я выполню его и пришлю результат следующим сообщением — тогда продолжишь "
        "ответ уже с реальными данными. Пути к файлам — относительно твоего "
        "workspace ('app.py', 'src/main.py').",
        "",
        "Примеры (делай ИМЕННО так, а не отписывайся):",
        "  «посмотри сайт example.com» →",
        "    <tool>web_browse({\"url\": \"https://example.com\", \"question\": \"что это за сайт\"})</tool>",
        "  «установи скилл <ссылка>» →",
        "    <tool>install_skill({\"url\": \"<ссылка>\"})</tool>",
        "  «покажи мои файлы» →",
        "    <tool>list_dir({\"path\": \".\"})</tool>",
        "  «удали папку foo» →",
        "    <tool>delete_path({\"path\": \"foo\"})</tool>",
        "",
        "ЯЗЫК: пиши ТОЛЬКО по-русски. НИ ОДНОГО китайского иероглифа / CJK — "
        "ни в тексте, ни в комментариях. Это критично.",
        "",
        "ВАЖНО про пути и файлы:",
        "- Ты работаешь на МАКЕ пользователя (через агента). Файлы создаются "
        "РЕАЛЬНО на его Mac, в разрешённых папках (allowlist). Это не песочница "
        "на сервере — это его настоящий компьютер.",
        "- Пути относительны корня рабочей папки. Передавай их РОВНО как показал "
        "list_dir — БЕЗ префикса 'workspace/'.",
        "- ФАЙЛ создаёшь через write_file({\"path\":\"main.py\",\"content\":\"...\"}). "
        "ПАПКУ — через mkdir({\"path\":\"src\"}). НЕ создавай папку через "
        "write_file и не создавай файл, когда просят папку.",
        "- Если инструмент вернул ошибку на каком-то пути — НЕ повторяй тот "
        "же вызов с тем же путём, исправь путь.",
        "- Когда просят создать проект, сайт или файл — РЕАЛЬНО создавай "
        "файлы через write_file (index.html, style.css, app.js, main.py и т.д.), "
        "а не просто показывай код в чате. Иначе ничего не сохранится.",
        "",
        "ОБЯЗАТЕЛЬНАЯ САМОПРОВЕРКА (для кода и файлов):",
        "- После того как создал или изменил файл — ПЕРЕЧИТАЙ его через "
        "read_file({\"path\":\"...\"}) и убедись, что содержимое записалось верно "
        "и полностью (нет обрезки, синтаксис на месте). Если что-то не так — "
        "исправь и перечитай снова.",
        "- Создал папку — проверь list_dir, что она появилась.",
        "- Не утверждай «готово/создал», пока сам не убедился инструментом, что "
        "это правда так.",
        "",
        "Доступные инструменты:",
    ]
    mcp_names: list[str] = []
    for name in enabled_tool_names:
        # External MCP tools (mcp__server__tool) have no local spec — list
        # them separately with a generic hint so the model still calls them.
        if name.startswith("mcp__"):
            mcp_names.append(name)
            continue
        entry = _BUILTIN_TOOLS.get(name)
        if not entry:
            continue
        param_doc = ", ".join(
            f"{k}: {v}" for k, v in (entry.get("params") or {}).items()
        )
        lines.append(f"  • {name} — {entry['description']} ({param_doc})")
    if mcp_names:
        lines.append("")
        lines.append(
            "Внешние MCP-инструменты (вызывай так же, аргументы — как требует "
            "сервер; имя содержит сервер и инструмент):"
        )
        for name in mcp_names:
            lines.append(f"  • {name}")
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
