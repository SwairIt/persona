"""Phase 2 — persistent Playwright browser worker (one per chat session).

Runs as a SUBPROCESS (``python -m app.browse.agent.worker``), never in the
web server's event loop — headless Chromium under uvicorn's
SelectorEventLoop cannot be driven by ``asyncio.create_subprocess_*`` on
Windows, so the parent talks to us over plain stdin/stdout pipes via a
background reader thread.

Protocol — line-delimited JSON (one compact object per line):

    parent → worker (request):
        {"id": 1, "cmd": "open",  "url": "https://x"}
        {"id": 2, "cmd": "click", "selector": "text=Войти"}
        {"id": 3, "cmd": "type",  "selector": "#q", "text": "hi", "enter": true}
        {"id": 4, "cmd": "read",  "selector": "main"}      # selector optional
        {"id": 5, "cmd": "screenshot", "path": "C:/.../shot.png", "full_page": true,
         "exec_id": 42, "rel_path": "browse/agent-1-...png"}   # exec_id/rel_path опц.
        {"id": 6, "cmd": "close"}
        {"id": 7, "cmd": "ping"}

    worker → parent (response, always echoes ``id``):
        {"id": 1, "ok": true,  "title": "...", "url": "...", "status": 200}
        {"id": 4, "ok": true,  "text": "...", "title": "...", "url": "..."}
        {"id": 2, "ok": false, "error": "selector not found: ..."}

The first line the worker prints (before any request) is a readiness
banner ``{"event":"ready"}`` or ``{"event":"fatal","error":...}`` so the
parent can fail fast if Playwright/Chromium is missing.

Everything here is synchronous Playwright (``sync_api``) — the process is
single-threaded and serves one request at a time, which is exactly what a
per-session interactive browser needs.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Hard caps so a single page can't hang the worker forever.
_NAV_TIMEOUT_MS = 30_000
_ACTION_TIMEOUT_MS = 15_000
_READ_CHARS_CAP = 12_000
_VIEWPORT = {"width": 1280, "height": 900}


def _emit(obj: dict[str, Any]) -> None:
    """Write one compact JSON line to stdout and flush immediately."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _record_artifact(req: dict[str, Any], art_type: str, mime_type: str) -> None:
    """Best-effort: привязать файл-артефакт к строке журнала активности (F6-05).

    Воркер — отдельный синхронный процесс без event-loop, поэтому пишет напрямую
    через ``add_artifact_sync`` (обычный sqlite3). Линковка к строке журнала идёт
    через ``exec_id`` + относительный ``rel_path`` внутри воркспейса — оба
    приходят из родителя в запросе (опционально). Любой сбой — тихий no-op,
    скриншот и ответ воркеру не ломаются.
    """
    exec_id = req.get("exec_id")
    rel_path = req.get("rel_path")
    if exec_id is None or not rel_path:
        return
    try:
        from app.activity.store import add_artifact_sync  # noqa: PLC0415

        add_artifact_sync(int(exec_id), art_type, mime_type, str(rel_path))
    except Exception:  # noqa: BLE001, S110 — best-effort, воркер живёт дальше
        pass


def main() -> int:  # noqa: PLR0915, C901 — long but flat dispatch loop
    try:
        from playwright.sync_api import (  # noqa: PLC0415
            Error as PWError,
        )
        from playwright.sync_api import (
            TimeoutError as PWTimeout,
        )
        from playwright.sync_api import (
            sync_playwright,
        )
    except ImportError:
        _emit({"event": "fatal", "error": "playwright не установлен (uv pip install playwright)"})
        return 3

    try:
        pw = sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        _emit({"event": "fatal", "error": f"playwright не стартовал: {exc}"})
        return 3

    browser = None
    page = None
    try:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport=_VIEWPORT)
        ctx.set_default_timeout(_ACTION_TIMEOUT_MS)
        ctx.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
        page = ctx.new_page()
    except Exception as exc:  # noqa: BLE001
        _emit({"event": "fatal", "error": f"не смог открыть Chromium: {exc}"})
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001, S110
                pass
        pw.stop()
        return 1

    _emit({"event": "ready"})

    # ---- request loop --------------------------------------------------
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _emit({"id": None, "ok": False, "error": "плохой JSON в запросе"})
            continue
        rid = req.get("id")
        cmd = str(req.get("cmd", "")).strip().lower()

        try:
            if cmd == "ping":
                _emit({"id": rid, "ok": True})

            elif cmd == "open":
                url = str(req.get("url", "")).strip()
                if not url:
                    _emit({"id": rid, "ok": False, "error": "нужен url"})
                    continue
                resp = page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(800)  # let late content paint
                _emit({
                    "id": rid, "ok": True,
                    "title": page.title(), "url": page.url,
                    "status": (resp.status if resp else None),
                })

            elif cmd == "click":
                sel = str(req.get("selector", "")).strip()
                if not sel:
                    _emit({"id": rid, "ok": False, "error": "нужен selector"})
                    continue
                page.click(sel)
                page.wait_for_timeout(500)
                _emit({"id": rid, "ok": True, "title": page.title(), "url": page.url})

            elif cmd == "type":
                sel = str(req.get("selector", "")).strip()
                text = str(req.get("text", ""))
                if not sel:
                    _emit({"id": rid, "ok": False, "error": "нужен selector"})
                    continue
                page.fill(sel, text)
                if req.get("enter"):
                    page.press(sel, "Enter")
                    page.wait_for_timeout(600)
                _emit({"id": rid, "ok": True, "url": page.url})

            elif cmd == "read":
                sel = str(req.get("selector", "")).strip()
                if sel:
                    el = page.query_selector(sel)
                    text = (el.inner_text() if el else "") or ""
                    if not el:
                        _emit({"id": rid, "ok": False,
                               "error": f"селектор не найден: {sel}"})
                        continue
                else:
                    text = page.inner_text("body")
                text = text.strip()[:_READ_CHARS_CAP]
                _emit({"id": rid, "ok": True, "text": text,
                       "title": page.title(), "url": page.url})

            elif cmd == "screenshot":
                out = str(req.get("path", "")).strip()
                if not out:
                    _emit({"id": rid, "ok": False, "error": "нужен path"})
                    continue
                page.screenshot(path=out, full_page=bool(req.get("full_page", True)))
                # F6-05: best-effort линк артефакта к строке активности (exec_id).
                # Не влияет на ответ воркеру/SSE — при сбое просто нет превью.
                _record_artifact(req, "screenshot", "image/png")
                _emit({"id": rid, "ok": True, "path": out,
                       "rel_path": req.get("rel_path"),
                       "title": page.title(), "url": page.url})

            elif cmd == "close":
                _emit({"id": rid, "ok": True})
                break

            else:
                _emit({"id": rid, "ok": False, "error": f"неизвестная команда: {cmd}"})

        except PWTimeout as exc:
            _emit({"id": rid, "ok": False, "error": f"таймаут: {str(exc)[:200]}"})
        except PWError as exc:
            _emit({"id": rid, "ok": False, "error": f"playwright: {str(exc)[:200]}"})
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            _emit({"id": rid, "ok": False,
                   "error": f"{type(exc).__name__}: {str(exc)[:200]}"})

    # ---- teardown ------------------------------------------------------
    try:
        if browser is not None:
            browser.close()
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        pw.stop()
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
