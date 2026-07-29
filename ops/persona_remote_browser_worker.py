"""Persona remote browser worker for the owner's Windows PC.

The process opens no listening port.  It long-polls Persona over HTTPS,
executes one of seven fixed Playwright actions, and returns a bounded JSON
result.  Browser state lives in PC-local persistent profiles so cookies survive
worker restarts.  No server payload is ever interpreted as Python, JavaScript,
a shell command, or a filesystem path.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit

try:
    import httpx
    from playwright.sync_api import (
        BrowserContext,
        Page,
        sync_playwright,
    )
    from playwright.sync_api import (
        Error as PlaywrightError,
    )
    from playwright.sync_api import (
        TimeoutError as PlaywrightTimeout,
    )
except ImportError:
    sys.stderr.write(
        "[persona-browser] Dependencies missing. Run: "
        "python -m pip install httpx playwright && python -m playwright install chromium\n"
    )
    raise SystemExit(2) from None

_ACTIONS: Final[frozenset[str]] = frozenset(
    {"open", "click", "type", "read", "screenshot", "close", "ping"}
)
_FIELDS: Final[dict[str, frozenset[str]]] = {
    "open": frozenset({"url"}),
    "click": frozenset({"selector"}),
    "type": frozenset({"selector", "text", "enter"}),
    "read": frozenset({"selector"}),
    "screenshot": frozenset({"full_page"}),
    "close": frozenset(),
    "ping": frozenset(),
}
_WORKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_MAX_URL = 2_048
_MAX_SELECTOR = 1_024
_MAX_INPUT = 16_384
_MAX_READ = 32_000
_MAX_SCREENSHOT_RAW = 1_450_000
_MAX_RESULT_BYTES = 2 * 1024 * 1024
_MAX_SESSIONS = 4
_MAX_STEPS = 60
_IDLE_SECONDS = 15 * 60
_POLL_WAIT = 25
_BACKOFF_MAX = 30.0
_MAX_POLICY_DOMAINS = 128
_MAX_DOMAIN_CHARS = 253
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def _read_dotenv() -> dict[str, str]:
    result: dict[str, str] = {}
    path = Path(__file__).resolve().with_name(".env")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in raw.splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, _, value = clean.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _cfg(dotenv: dict[str, str], name: str, default: str = "") -> str:
    return (os.environ.get(name) or dotenv.get(name) or default).strip()


class Config:
    def __init__(self) -> None:
        dotenv = _read_dotenv()
        self.server = _cfg(
            dotenv, "PERSONA_SERVER", "https://persona.getdoday.ru"
        ).rstrip("/")
        self.token = _cfg(dotenv, "PERSONA_BROWSER_WORKER_TOKEN")
        raw_worker = _cfg(
            dotenv,
            "PERSONA_BROWSER_WORKER_ID",
            f"{socket.gethostname()}-browser",
        )
        if not _WORKER_RE.fullmatch(raw_worker):
            raw_worker = f"browser-{hashlib.sha256(raw_worker.encode()).hexdigest()[:16]}"
        self.worker_id = raw_worker
        default_profiles = (
            Path(os.environ.get("LOCALAPPDATA") or Path.home())
            / "Persona"
            / "browser-profiles"
        )
        self.profiles = Path(
            _cfg(dotenv, "PERSONA_BROWSER_PROFILE_DIR", str(default_profiles))
        ).resolve()
        self.headless = _cfg(dotenv, "PERSONA_BROWSER_HEADLESS", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.browser_proxy = _cfg(dotenv, "PERSONA_BROWSER_PROXY")
        heartbeat = _cfg(dotenv, "PERSONA_BROWSER_HEARTBEAT_FILE")
        self.heartbeat_file = Path(heartbeat) if heartbeat else None
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
            value = _cfg(dotenv, name)
            if value and not os.environ.get(name):
                os.environ[name] = value
        if not self.token:
            raise ValueError("PERSONA_BROWSER_WORKER_TOKEN is required")
        if not self.server.startswith("https://") and not _is_loopback_server(
            self.server
        ):
            raise ValueError("PERSONA_SERVER must use HTTPS")
        self.profiles.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    allow_domains: frozenset[str]
    deny_domains: frozenset[str]
    block_all: bool


@dataclass(slots=True)
class NetworkGuard:
    policy: NetworkPolicy

    def route(self, route: Any, request: Any) -> None:
        parsed = urlsplit(request.url)
        if parsed.scheme in {"data", "blob", "about"}:
            route.continue_()
            return
        try:
            _require_url_allowed(request.url, self.policy)
        except ValueError:
            route.abort("blockedbyclient")
            return
        route.continue_()

    def websocket(self, ws_route: Any) -> None:
        try:
            url = str(ws_route.url).replace("wss://", "https://", 1).replace(
                "ws://", "http://", 1
            )
            _require_url_allowed(url, self.policy)
        except ValueError:
            ws_route.close(code=1008, reason="network policy")
            return
        ws_route.connect_to_server()


@dataclass(slots=True)
class Session:
    context: BrowserContext
    page: Page
    guard: NetworkGuard
    last_used: float
    steps: int = 0


class BrowserRuntime:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pw = sync_playwright().start()
        self._sessions: dict[str, Session] = {}

    def close(self) -> None:
        for key in list(self._sessions):
            self.close_session(key)
        self._pw.stop()

    def close_session(self, profile_key: str) -> None:
        session = self._sessions.pop(profile_key, None)
        if session is None:
            return
        with contextlib.suppress(Exception):
            session.context.close()

    def reap(self) -> None:
        now = time.monotonic()
        for key, session in list(self._sessions.items()):
            if now - session.last_used > _IDLE_SECONDS or session.steps >= _MAX_STEPS:
                self.close_session(key)

    def execute(self, job: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0911
        action, args, policy = _validate_job(job)
        key = str(job.get("profile_key") or "")
        if not re.fullmatch(r"owner-\d+-session-\d+", key):
            raise ValueError("invalid profile_key")
        if action == "close":
            self.close_session(key)
            return {"ok": True, "closed": True}
        session = self._get_session(key, str(job.get("resume_url") or ""), policy)
        if session.steps >= _MAX_STEPS:
            self.close_session(key)
            raise RuntimeError(f"browser session exceeded {_MAX_STEPS} steps")
        session.steps += 1
        session.last_used = time.monotonic()
        page = session.page

        if action == "ping":
            return {"ok": True, "title": page.title(), "url": page.url}
        if action == "open":
            url = str(args["url"])
            _require_url_allowed(url, policy)
            response = page.goto(url, wait_until="domcontentloaded")
            return {
                "ok": True,
                "title": page.title()[:1_000],
                "url": page.url[:_MAX_URL],
                "status": response.status if response else None,
            }
        if action == "click":
            page.locator(str(args["selector"])).first.click()
            return {
                "ok": True,
                "title": page.title()[:1_000],
                "url": page.url[:_MAX_URL],
            }
        if action == "type":
            locator = page.locator(str(args["selector"])).first
            locator.fill(str(args["text"]))
            if args["enter"]:
                locator.press("Enter")
            return {
                "ok": True,
                "title": page.title()[:1_000],
                "url": page.url[:_MAX_URL],
            }
        if action == "read":
            selector = str(args["selector"])
            text = (
                page.locator(selector).first.inner_text()
                if selector
                else page.locator("body").inner_text()
            )
            return {
                "ok": True,
                "text": text[:_MAX_READ],
                "truncated": len(text) > _MAX_READ,
                "title": page.title()[:1_000],
                "url": page.url[:_MAX_URL],
            }
        if action == "screenshot":
            return self._screenshot(page, bool(args["full_page"]))
        raise ValueError(f"unsupported action: {action}")

    def _get_session(
        self,
        profile_key: str,
        resume_url: str,
        policy: NetworkPolicy,
    ) -> Session:
        existing = self._sessions.get(profile_key)
        if existing is not None:
            existing.guard.policy = policy
            return existing
        self.reap()
        if len(self._sessions) >= _MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].last_used)
            self.close_session(oldest)

        digest = hashlib.sha256(profile_key.encode("utf-8")).hexdigest()
        profile_dir = (self.cfg.profiles / digest).resolve()
        if self.cfg.profiles not in profile_dir.parents:
            raise RuntimeError("profile path escaped the configured root")
        profile_dir.mkdir(parents=True, exist_ok=True)
        launch: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": self.cfg.headless,
            "viewport": {"width": 1280, "height": 900},
            "args": ["--no-first-run", "--disable-background-networking"],
        }
        proxy = _proxy_settings(self.cfg.browser_proxy)
        if proxy:
            launch["proxy"] = proxy
        context = self._pw.chromium.launch_persistent_context(**launch)
        context.set_default_timeout(15_000)
        context.set_default_navigation_timeout(35_000)
        guard = NetworkGuard(policy)
        context.route("**/*", guard.route)
        _route_websockets(context, guard)
        page = context.pages[0] if context.pages else context.new_page()
        session = Session(
            context=context,
            page=page,
            guard=guard,
            last_used=time.monotonic(),
        )
        self._sessions[profile_key] = session
        if resume_url and page.url == "about:blank":
            try:
                _require_url_allowed(resume_url, policy)
                page.goto(resume_url, wait_until="domcontentloaded")
            except Exception as exc:
                log(f"could not restore session page: {type(exc).__name__}")
        return session

    def _screenshot(self, page: Page, full_page: bool) -> dict[str, Any]:
        raw = page.screenshot(type="jpeg", quality=70, full_page=full_page)
        if len(raw) > _MAX_SCREENSHOT_RAW:
            raw = page.screenshot(type="jpeg", quality=50, full_page=False)
        if len(raw) > _MAX_SCREENSHOT_RAW:
            raise RuntimeError("screenshot exceeds the result size limit")
        result = {
            "ok": True,
            "screenshot_base64": base64.b64encode(raw).decode("ascii"),
            "mime_type": "image/jpeg",
            "title": page.title()[:1_000],
            "url": page.url[:_MAX_URL],
        }
        if len(json.dumps(result, separators=(",", ":")).encode()) > _MAX_RESULT_BYTES:
            raise RuntimeError("encoded screenshot exceeds the result size limit")
        return result


def _validate_job(job: object) -> tuple[str, dict[str, Any], NetworkPolicy]:
    if not isinstance(job, dict):
        raise ValueError("job must be an object")
    allowed_job_fields = {
        "job_id",
        "owner_user_id",
        "session_id",
        "profile_key",
        "resume_url",
        "action",
        "arguments",
        "lease_seconds",
        "network_policy",
    }
    unknown_job_fields = set(job) - allowed_job_fields
    if unknown_job_fields:
        raise ValueError(
            f"unsupported job fields: {', '.join(sorted(unknown_job_fields))}"
        )
    action = str(job.get("action") or "").lower()
    args_raw = job.get("arguments")
    if action not in _ACTIONS or not isinstance(args_raw, dict):
        raise ValueError("invalid browser action")
    policy = _validate_network_policy(job.get("network_policy"))
    unknown = set(args_raw) - _FIELDS[action]
    if unknown:
        raise ValueError(f"unsupported action fields: {', '.join(sorted(unknown))}")
    args = dict(args_raw)
    if action == "open":
        _string(args.get("url"), "url", _MAX_URL)
        _require_url_allowed(str(args["url"]), policy)
    elif action == "click":
        _string(args.get("selector"), "selector", _MAX_SELECTOR)
    elif action == "type":
        _string(args.get("selector"), "selector", _MAX_SELECTOR)
        _string(args.get("text"), "text", _MAX_INPUT, empty=True)
        _boolean(args.get("enter"), "enter")
    elif action == "read":
        _string(args.get("selector"), "selector", _MAX_SELECTOR, empty=True)
    elif action == "screenshot":
        _boolean(args.get("full_page"), "full_page")
    return action, args, policy


def _validate_network_policy(raw: object) -> NetworkPolicy:
    if not isinstance(raw, dict):
        raise ValueError("network_policy is required")
    if set(raw) != {"version", "allow_domains", "deny_domains", "block_all"}:
        raise ValueError("invalid network_policy fields")
    if raw.get("version") != 1 or type(raw.get("block_all")) is not bool:
        raise ValueError("unsupported network_policy")

    def domains(name: str) -> frozenset[str]:
        values = raw.get(name)
        if not isinstance(values, list) or len(values) > _MAX_POLICY_DOMAINS:
            raise ValueError(f"{name} is invalid")
        clean: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                raise ValueError(f"{name} is invalid")
            candidate = value.lower().rstrip(".")
            if (
                len(candidate) > _MAX_DOMAIN_CHARS
                or not _DOMAIN_RE.fullmatch(candidate)
            ):
                raise ValueError(f"{name} is invalid")
            clean.add(candidate)
        return frozenset(clean)

    return NetworkPolicy(
        allow_domains=domains("allow_domains"),
        deny_domains=domains("deny_domains"),
        block_all=bool(raw["block_all"]),
    )


def _string(
    value: object, name: str, maximum: int, *, empty: bool = False
) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _require_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("only absolute public http(s) URLs are allowed")
    host = parsed.hostname.rstrip(".")
    try:
        addresses = {
            str(info[4][0])
            for info in socket.getaddrinfo(host, parsed.port)
        }
    except OSError as exc:
        raise ValueError(f"DNS lookup failed for {host}") from exc
    if not addresses or any(_non_public_ip(value) for value in addresses):
        raise ValueError("private, local, link-local and reserved networks are blocked")


def _require_url_allowed(url: str, policy: NetworkPolicy) -> None:
    _require_public_url(url)
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if policy.block_all:
        raise ValueError("browser network policy is fail-closed")
    if _host_matches(host, policy.deny_domains):
        raise ValueError(f"domain is denied by owner policy: {host}")
    if policy.allow_domains and not _host_matches(host, policy.allow_domains):
        raise ValueError(f"domain is not allowed by owner policy: {host}")


def _host_matches(host: str, patterns: frozenset[str]) -> bool:
    return any(host == pattern or host.endswith("." + pattern) for pattern in patterns)


def _non_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    return not ip.is_global


def _route_websockets(context: BrowserContext, guard: NetworkGuard) -> None:
    method = getattr(context, "route_web_socket", None)
    if method is None:
        return

    method("**/*", guard.websocket)


def _proxy_settings(explicit: str) -> dict[str, str] | None:
    raw = (
        explicit
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("ALL_PROXY")
        or ""
    ).strip()
    if not raw:
        # Chromium inherits the Windows system proxy/VPN when no explicit
        # Playwright proxy is supplied.
        return None
    parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
    if not parsed.hostname:
        raise ValueError("invalid PERSONA_BROWSER_PROXY")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5"}:
        raise ValueError("browser proxy must use http(s) or socks5")
    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    result = {"server": server}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


def _is_loopback_server(server: str) -> bool:
    try:
        return ipaddress.ip_address(urlsplit(server).hostname or "").is_loopback
    except ValueError:
        return (urlsplit(server).hostname or "").lower() == "localhost"


def _heartbeat(
    client: httpx.Client, cfg: Config, job_id: int
) -> bool:
    response = client.post(
        f"{cfg.server}/api/llm/worker/browser/{job_id}/heartbeat",
        headers={"X-Worker-Token": cfg.token},
        json={"worker_id": cfg.worker_id},
        timeout=20.0,
    )
    response.raise_for_status()
    return bool(response.json().get("cancel_requested"))


def _finish(
    client: httpx.Client,
    cfg: Config,
    job_id: int,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    body: dict[str, Any] = {"worker_id": cfg.worker_id}
    if error is None:
        body["result"] = result or {}
    else:
        body["error"] = error[:2_000]
    response = client.post(
        f"{cfg.server}/api/llm/worker/browser/{job_id}/done",
        headers={"X-Worker-Token": cfg.token},
        json=body,
        timeout=45.0,
    )
    if response.status_code != 409:
        response.raise_for_status()


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def _mark_successful_poll(path: Path | None) -> None:
    if path is None:
        return
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(str(time.time()), encoding="ascii")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)


class Stop:
    requested = False

    def __call__(self, *_args: object) -> None:
        self.requested = True


def main() -> int:
    try:
        cfg = Config()
    except ValueError as exc:
        log(f"configuration error: {exc}")
        return 2
    stop = Stop()
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    runtime = BrowserRuntime(cfg)
    backoff = 1.0
    timeout = httpx.Timeout(_POLL_WAIT + 15.0, connect=20.0)
    log(
        f"outbound browser worker started as {cfg.worker_id}; "
        f"visible_browser={not cfg.headless}"
    )
    try:
        with httpx.Client(timeout=timeout, trust_env=True) as client:
            while not stop.requested:
                try:
                    response = client.get(
                        f"{cfg.server}/api/llm/worker/browser/next",
                        headers={"X-Worker-Token": cfg.token},
                        params={"worker_id": cfg.worker_id, "wait": _POLL_WAIT},
                    )
                    if response.status_code in {200, 204}:
                        _mark_successful_poll(cfg.heartbeat_file)
                    if response.status_code == 204:
                        runtime.reap()
                        backoff = 1.0
                        continue
                    response.raise_for_status()
                    job = response.json()
                    job_id = int(job["job_id"])
                    backoff = 1.0
                    if _heartbeat(client, cfg, job_id):
                        _finish(client, cfg, job_id, error="cancelled before execution")
                        continue
                    try:
                        result = runtime.execute(job)
                    except (ValueError, RuntimeError, PlaywrightError, PlaywrightTimeout) as exc:
                        _finish(
                            client,
                            cfg,
                            job_id,
                            error=f"{type(exc).__name__}: {exc!s}",
                        )
                        continue
                    if _heartbeat(client, cfg, job_id):
                        _finish(client, cfg, job_id, error="cancelled during execution")
                    else:
                        _finish(client, cfg, job_id, result=result)
                except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
                    log(f"server unavailable ({type(exc).__name__}); retry in {backoff:g}s")
                    time.sleep(backoff)
                    backoff = min(_BACKOFF_MAX, backoff * 2)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
