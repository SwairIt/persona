"""Phase 2 — interactive browser-agent tools (persistent Playwright worker).

Unlike the one-shot ``web_browse`` builtin (open → screenshot → close), these
tools keep a Chromium worker ALIVE for the whole chat session so the model
can click, type, read and re-screenshot across turns — a real automation
loop. The heavy lifting (subprocess, pipes, domain policy, idle-TTL, step
cap) lives in :mod:`app.browse.agent.manager`; this module is the thin
tool-facing layer that the MCP dispatcher (:func:`app.mcp.call_tool`) calls.

Every tool takes ``(args, user_id, session_id)`` so it can bind to the
right session worker and write screenshots into the right user workspace.
Screenshots are surfaced to the activity window via ``publish_activity``
(best-effort — a logging failure never fails the tool).

These tools are OPTIONAL: they are only offered to the model when the
``builtin:browser_open`` etc. rows are enabled in ``mcp_server`` AND the
``browser_backend`` kv is ``builtin`` or ``both`` (see
:mod:`app.web.routes.automation_settings`).
"""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Any

from app.browse.agent import manager
from app.logging_setup import get_logger

log = get_logger("persona.browse.agent.tools")


def _need_session(session_id: int | None) -> int | None:
    """Browser tools are session-scoped; return a usable id or None."""
    return session_id if session_id and session_id > 0 else None


async def _publish_shot(session_id: int, rel_path: str, title: str) -> None:
    """Best-effort: announce a fresh screenshot to the live activity window."""
    try:
        from app.web.routes.live_sse import publish_activity  # noqa: PLC0415

        await publish_activity({
            "session_id": session_id,
            "tool": "browser_screenshot",
            "status": "done",
            "kind": "browser",
            "artifact": f"/workspace/file/{rel_path}",
            "title": title,
        })
    except Exception as exc:  # noqa: BLE001 — fire-and-forget
        log.debug("browse.agent.publish_failed", error=str(exc))


def _fmt(res: dict[str, Any], ok_prefix: str) -> str:
    """Turn a worker response dict into an LLM-facing ``[ok]/[error]`` string."""
    if not res.get("ok"):
        return f"[error] {res.get('error', 'браузер вернул ошибку')}"
    bits = [ok_prefix]
    if res.get("title"):
        bits.append(f"Заголовок: {res['title']}")
    if res.get("url"):
        bits.append(f"URL: {res['url']}")
    if res.get("status") is not None:
        bits.append(f"HTTP {res['status']}")
    return "[ok] " + " | ".join(bits)


async def browser_open(
    args: dict[str, Any], user_id: int = 0, session_id: int | None = None
) -> str:
    """Открыть URL в постоянном браузере этой сессии (живёт между ходами)."""
    sid = _need_session(session_id)
    if sid is None:
        return "[error] браузер-агент работает только внутри чат-сессии"
    url = str(args.get("url", "")).strip()
    ok, norm, why = await manager.check_url(url)
    if not ok:
        return f"[error] {why}"
    res = await manager.run(sid, "open", user_id=user_id, url=norm)
    return _fmt(res, f"Открыл {norm}")


async def browser_click(
    args: dict[str, Any], user_id: int = 0, session_id: int | None = None
) -> str:
    """Кликнуть по элементу (CSS-селектор или 'text=Кнопка')."""
    sid = _need_session(session_id)
    if sid is None:
        return "[error] браузер-агент работает только внутри чат-сессии"
    sel = str(args.get("selector") or args.get("sel") or args.get("element") or "").strip()
    if not sel:
        return "[error] нужен selector (например '#login' или 'text=Войти')"
    res = await manager.run(sid, "click", user_id=user_id, selector=sel)
    return _fmt(res, f"Кликнул по {sel}")


async def browser_type(
    args: dict[str, Any], user_id: int = 0, session_id: int | None = None
) -> str:
    """Ввести текст в поле (selector + text[, enter])."""
    sid = _need_session(session_id)
    if sid is None:
        return "[error] браузер-агент работает только внутри чат-сессии"
    sel = str(args.get("selector") or args.get("sel") or args.get("field") or "").strip()
    text = str(args.get("text") or args.get("value") or args.get("query") or "")
    if not sel:
        return "[error] нужен selector поля ввода"
    enter = bool(args.get("enter") or args.get("submit"))
    res = await manager.run(
        sid,
        "type",
        user_id=user_id,
        selector=sel,
        text=text,
        enter=enter,
    )
    return _fmt(res, f"Ввёл текст в {sel}" + (" + Enter" if enter else ""))


async def browser_read(
    args: dict[str, Any], user_id: int = 0, session_id: int | None = None
) -> str:
    """Прочитать видимый текст страницы или конкретного элемента (selector опц.)."""
    sid = _need_session(session_id)
    if sid is None:
        return "[error] браузер-агент работает только внутри чат-сессии"
    sel = str(args.get("selector") or args.get("sel") or "").strip()
    res = await manager.run(sid, "read", user_id=user_id, selector=sel)
    if not res.get("ok"):
        return f"[error] {res.get('error', 'не смог прочитать')}"
    text = res.get("text", "") or "(пусто)"
    return f"[ok] Текст страницы ({res.get('url', '')}):\n{text}"


async def browser_screenshot(
    args: dict[str, Any], user_id: int = 0, session_id: int | None = None
) -> str:
    """Сделать скриншот текущей страницы → файл в workspace + показ в активности."""
    sid = _need_session(session_id)
    if sid is None:
        return "[error] браузер-агент работает только внутри чат-сессии"
    from app.workspace import ensure_user_workspace  # noqa: PLC0415

    ws = ensure_user_workspace(user_id)
    bdir = ws / "browse"
    try:
        bdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"[error] не смог создать папку для скринов: {exc}"
    extension = "jpg" if await manager.browser_backend() == "remote" else "png"
    out = bdir / f"agent-{sid}-{int(_time.time())}.{extension}"
    full_page = bool(args.get("full_page", True))
    res = await manager.run(
        sid,
        "screenshot",
        user_id=user_id,
        path=str(out),
        full_page=full_page,
    )
    if not res.get("ok"):
        return f"[error] {res.get('error', 'скриншот не удался')}"
    rel = Path(out).relative_to(ws).as_posix()
    await _publish_shot(sid, rel, str(res.get("title", "")))
    return (
        f"[ok] Скриншот сохранён: /workspace/file/{rel}\n"
        f"Заголовок: {res.get('title', '')} | URL: {res.get('url', '')}"
    )


async def browser_close(
    args: dict[str, Any], user_id: int = 0, session_id: int | None = None
) -> str:
    """Закрыть браузер этой сессии (освободить процесс Chromium)."""
    sid = _need_session(session_id)
    if sid is None:
        return "[ok] браузер не был открыт"
    res = await manager.run(sid, "close", user_id=user_id)
    return "[ok] браузер закрыт" if res.get("ok") else f"[error] {res.get('error')}"


# Registry fragment merged into builtin_tools._BUILTIN_TOOLS at import time.
BROWSER_AGENT_TOOLS: dict[str, dict[str, Any]] = {
    "browser_open": {
        "fn": browser_open,
        "description": (
            "Открыть URL в ПОСТОЯННОМ браузере (живёт всю сессию — можно кликать, "
            "вводить текст, читать, делать скрин между ходами). Используй для "
            "интерактивных задач на сайте (логин, заполнить форму, пройти по шагам)."
        ),
        "params": {"url": "адрес страницы (https://...)"},
    },
    "browser_click": {
        "fn": browser_click,
        "description": "Кликнуть по элементу в открытом браузере (CSS или 'text=Текст').",
        "params": {"selector": "CSS-селектор или text=Подпись"},
    },
    "browser_type": {
        "fn": browser_type,
        "description": "Ввести текст в поле открытой страницы (enter=true чтобы отправить).",
        "params": {"selector": "селектор поля", "text": "что ввести",
                   "enter": "необяз.: true нажать Enter"},
    },
    "browser_read": {
        "fn": browser_read,
        "description": "Прочитать видимый текст страницы (или элемента по selector).",
        "params": {"selector": "необяз.: CSS-селектор куска страницы"},
    },
    "browser_screenshot": {
        "fn": browser_screenshot,
        "description": "Снять скриншот текущей страницы открытого браузера (→ файл + активность).",
        "params": {"full_page": "необяз.: true (вся страница) | false (только видимая область)"},
    },
    "browser_close": {
        "fn": browser_close,
        "description": "Закрыть постоянный браузер сессии (освободить ресурсы). Вызови в конце.",
        "params": {},
    },
}

# Hallucination aliases — weak models invent names for browser actions.
BROWSER_AGENT_ALIASES: dict[str, str] = {
    "browser_navigate": "browser_open", "navigate": "browser_open",
    "goto": "browser_open", "open_browser": "browser_open", "browser_goto": "browser_open",
    "click": "browser_click", "browser_press": "browser_click", "tap": "browser_click",
    "fill": "browser_type", "browser_fill": "browser_type", "input": "browser_type",
    "browser_input": "browser_type", "browser_text": "browser_read",
    "read_page": "browser_read", "get_text": "browser_read", "page_text": "browser_read",
    "snapshot": "browser_screenshot", "browser_snapshot": "browser_screenshot",
    "screenshot": "browser_screenshot", "browser_shot": "browser_screenshot",
    "shutdown_browser": "browser_close", "close_browser": "browser_close",
}


__all__ = [
    "BROWSER_AGENT_ALIASES",
    "BROWSER_AGENT_TOOLS",
    "browser_click",
    "browser_close",
    "browser_open",
    "browser_read",
    "browser_screenshot",
    "browser_type",
]
