"""Security, lease and recovery contracts for the PC-local browser worker."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.remote_browser import credentials as browser_credentials
from app.adapters.remote_browser.gateway import _persist_screenshot
from app.adapters.remote_browser.repository import (
    MAX_RESULT_BYTES,
    RemoteBrowserJobStateError,
    SqliteRemoteBrowserJobs,
)
from app.application.automation.contracts import (
    BrowserAction,
    BrowserActionError,
    BrowserCommand,
)
from app.application.automation.service import (
    BrowserExecutionTimeout,
    RemoteBrowserService,
)
from app.browse.agent import manager as browser_manager
from app.llm import worker_queue
from app.storage.db import get_connection
from app.web.routes import remote_browser_worker as worker_routes
from app.workspace import ensure_user_workspace
from ops import persona_remote_browser_worker as pc_worker


def _command(
    *,
    correlation_id: str = "corr-1",
    session_id: int = 10,
    action: str = "open",
    arguments: dict[str, Any] | None = None,
    owner_user_id: int = 1,
    is_owner: bool = True,
) -> BrowserCommand:
    return BrowserCommand(
        owner_user_id=owner_user_id,
        session_id=session_id,
        action=BrowserAction.parse(
            action,
            arguments if arguments is not None else {"url": "https://example.com"},
        ),
        correlation_id=correlation_id,
        is_owner=is_owner,
    )


def test_browser_action_schema_is_exact_and_bounded() -> None:
    assert BrowserAction.parse("read", {}).arguments == {"selector": ""}
    assert BrowserAction.parse("screenshot", {}).arguments == {"full_page": True}
    with pytest.raises(BrowserActionError):
        BrowserAction.parse("shell", {"command": "whoami"})
    with pytest.raises(BrowserActionError):
        BrowserAction.parse("open", {"url": "file:///etc/passwd"})
    with pytest.raises(BrowserActionError):
        BrowserAction.parse("open", {"url": "https://user:secret@example.com"})
    with pytest.raises(BrowserActionError):
        BrowserAction.parse("click", {"selector": "#ok", "javascript": "alert(1)"})
    with pytest.raises(BrowserActionError):
        BrowserAction.parse("type", {"selector": "#q", "text": "x", "enter": 1})
    with pytest.raises(BrowserActionError):
        BrowserAction.parse("type", {"selector": "#q", "text": "x" * 16_385})


def test_non_owner_cannot_create_remote_browser_command() -> None:
    with pytest.raises(PermissionError):
        _command(is_owner=False)


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_correlation_cannot_be_repurposed(db) -> None:
    repository = SqliteRemoteBrowserJobs()
    first = await repository.enqueue(_command())
    second = await repository.enqueue(_command())
    assert second == first

    with pytest.raises(RemoteBrowserJobStateError):
        await repository.enqueue(
            _command(
                correlation_id="corr-1",
                action="read",
                arguments={},
            )
        )


@pytest.mark.asyncio
async def test_atomic_claim_binds_session_to_exactly_one_worker(db) -> None:
    repository = SqliteRemoteBrowserJobs()
    job_id = await repository.enqueue(_command())
    first, second = await asyncio.gather(
        repository.claim("owner-pc-a"),
        repository.claim("owner-pc-b"),
    )
    claimed = [job for job in (first, second) if job is not None]
    assert len(claimed) == 1
    assert claimed[0].id == job_id

    expected_worker = claimed[0].worker_id
    other_worker = "owner-pc-b" if expected_worker == "owner-pc-a" else "owner-pc-a"
    with pytest.raises(RemoteBrowserJobStateError):
        await repository.heartbeat(job_id, other_worker)
    with pytest.raises(RemoteBrowserJobStateError):
        await repository.finish(job_id, other_worker, result={"ok": True})


@pytest.mark.asyncio
async def test_worker_presence_is_observable_without_credentials(db) -> None:
    repository = SqliteRemoteBrowserJobs()
    assert await repository.worker_status() == {"online": False, "workers": []}

    await repository.touch_worker("owner-pc")

    status = await repository.worker_status()
    assert status["online"] is True
    assert status["workers"] == [
        {
            "worker_id": "owner-pc",
            "last_seen": status["workers"][0]["last_seen"],
            "online": True,
        }
    ]


@pytest.mark.asyncio
async def test_claimed_cancellation_is_acknowledged_without_late_result(db) -> None:
    repository = SqliteRemoteBrowserJobs()
    job_id = await repository.enqueue(_command())
    claimed = await repository.claim("owner-pc")
    assert claimed is not None

    assert await repository.cancel(job_id, "caller disconnected")
    assert await repository.heartbeat(job_id, "owner-pc") is True
    status = await repository.finish(
        job_id,
        "owner-pc",
        result={"ok": True, "text": "must not survive"},
    )
    assert status == "cancelled"
    job = await repository.get(job_id)
    assert job is not None
    assert job.status == "cancelled"
    assert job.result is None
    assert job.error == "cancelled by server"

    next_id = await repository.enqueue(
        _command(correlation_id="corr-2", action="read", arguments={})
    )
    assert await repository.claim("different-pc") is None
    next_job = await repository.claim("owner-pc")
    assert next_job is not None and next_job.id == next_id


@pytest.mark.asyncio
async def test_expired_lease_fails_job_and_releases_single_flight(db) -> None:
    repository = SqliteRemoteBrowserJobs()
    job_id = await repository.enqueue(_command())
    assert await repository.claim("owner-pc") is not None
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE remote_browser_job
               SET lease_until=datetime('now', '-1 second')
             WHERE id=?
            """,
            (job_id,),
        )
        await conn.commit()
    stats = await repository.maintain()
    assert stats["leases_expired"] == 1
    job = await repository.get(job_id)
    assert job is not None
    assert job.status == "error"
    assert "lost its lease" in (job.error or "")
    assert job.result is None
    assert job.action.arguments == {"url": "https://redacted.invalid"}


@pytest.mark.asyncio
async def test_abandoned_terminal_result_is_scrubbed_after_short_grace(db) -> None:
    repository = SqliteRemoteBrowserJobs()
    job_id = await repository.enqueue(_command())
    assert await repository.claim("owner-pc") is not None
    await repository.finish(
        job_id,
        "owner-pc",
        result={"ok": True, "text": "private page contents"},
    )
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE remote_browser_job
               SET finished_at=datetime('now', '-91 seconds')
             WHERE id=?
            """,
            (job_id,),
        )
        await conn.commit()

    await repository.maintain()

    job = await repository.get(job_id)
    assert job is not None
    assert job.result is None
    assert job.action.arguments == {"url": "https://redacted.invalid"}


@pytest.mark.asyncio
async def test_result_and_error_limits_are_enforced(db) -> None:
    repository = SqliteRemoteBrowserJobs()
    job_id = await repository.enqueue(_command())
    assert await repository.claim("owner-pc") is not None
    with pytest.raises(ValueError, match="exceeds"):
        await repository.finish(
            job_id,
            "owner-pc",
            result={"blob": "x" * (MAX_RESULT_BYTES + 1)},
        )
    # Failed validation did not consume the lease.
    assert await repository.heartbeat(job_id, "owner-pc") is False
    await repository.finish(
        job_id,
        "owner-pc",
        result={"ok": True, "text": "sensitive page contents"},
    )
    before_scrub = await repository.get(job_id)
    assert before_scrub is not None
    assert before_scrub.result == {"ok": True, "text": "sensitive page contents"}
    await repository.scrub_sensitive(job_id)
    after_scrub = await repository.get(job_id)
    assert after_scrub is not None
    assert after_scrub.result is None
    assert after_scrub.action.arguments == {"url": "https://redacted.invalid"}


class _NeverCompletes:
    def __init__(self) -> None:
        self.cancelled: list[tuple[int, str]] = []
        self.scrubbed: list[int] = []

    async def enqueue(self, command: BrowserCommand) -> int:
        return 42

    async def get(self, job_id: int):
        return type("Job", (), {"status": "pending"})()

    async def wait_for_change(self, job_id: int, timeout: float) -> bool:
        await asyncio.sleep(timeout)
        return False

    async def cancel(self, job_id: int, reason: str) -> bool:
        self.cancelled.append((job_id, reason))
        return True

    async def scrub_sensitive(self, job_id: int) -> None:
        self.scrubbed.append(job_id)

    def forget(self, job_id: int) -> None:
        return None


@pytest.mark.asyncio
async def test_application_timeout_requests_durable_cancel() -> None:
    port = _NeverCompletes()
    service = RemoteBrowserService(
        port, execution_timeout_seconds=0.01, poll_fallback_seconds=0.01
    )
    with pytest.raises(BrowserExecutionTimeout):
        await service.execute(_command())
    assert port.cancelled == [(42, "server execution timeout")]
    assert port.scrubbed == [42]


def test_http_worker_endpoints_fail_closed_and_limit_body(monkeypatch) -> None:
    async def validate(token: str) -> bool:
        return token == "owner-worker-secret"

    async def policy() -> dict[str, Any]:
        return {
            "version": 1,
            "allow_domains": ["example.com"],
            "deny_domains": [],
            "block_all": False,
        }

    monkeypatch.setattr(worker_routes, "validate_browser_worker_token", validate)
    monkeypatch.setattr(worker_routes, "browser_network_policy", policy)
    app = FastAPI()
    app.include_router(worker_routes.router)
    client = TestClient(app)

    denied = client.get("/api/llm/worker/browser/probe")
    assert denied.status_code == 401
    accepted = client.get(
        "/api/llm/worker/browser/probe",
        headers={"X-Worker-Token": "owner-worker-secret"},
    )
    assert accepted.status_code == 200
    assert "shell" not in accepted.json()["actions"]
    assert accepted.json()["network_policy"]["allow_domains"] == ["example.com"]

    too_large = client.post(
        "/api/llm/worker/browser/1/done",
        headers={
            "X-Worker-Token": "owner-worker-secret",
            "Content-Type": "application/json",
        },
        content=json.dumps(
            {
                "worker_id": "owner-pc",
                "result": {"blob": "x" * (MAX_RESULT_BYTES + 20_000)},
            }
        ),
    )
    assert too_large.status_code == 413


@pytest.mark.asyncio
async def test_browser_and_llm_worker_tokens_are_not_interchangeable(db) -> None:
    llm_token = await worker_queue.rotate_worker_token()
    browser_token = await browser_credentials.rotate_browser_worker_token()

    assert await worker_queue.validate_worker_token(llm_token)
    assert await browser_credentials.validate_browser_worker_token(browser_token)
    assert not await worker_queue.validate_worker_token(browser_token)
    assert not await browser_credentials.validate_browser_worker_token(llm_token)


def test_pc_worker_is_outbound_only_and_has_no_code_execution_primitives() -> None:
    source = Path("ops/persona_remote_browser_worker.py").read_text(encoding="utf-8")
    assert "launch_persistent_context" in source
    assert "trust_env=True" in source
    assert "PERSONA_BROWSER_PROXY" in source
    assert "subprocess" not in source
    assert "os.system" not in source
    assert "shell=True" not in source
    assert "eval(" not in source
    assert "exec(" not in source
    assert "listen(" not in source
    assert 'PERSONA_BROWSER_WORKER_TOKEN' in source
    assert '_cfg(dotenv, "PERSONA_WORKER_TOKEN")' not in source


def test_pc_worker_enforces_policy_on_every_network_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pc_worker, "_require_public_url", lambda _url: None)
    policy = pc_worker.NetworkPolicy(
        allow_domains=frozenset({"example.com"}),
        deny_domains=frozenset(),
        block_all=False,
    )
    guard = pc_worker.NetworkGuard(policy)

    class Route:
        continued = False
        aborted = False

        def continue_(self) -> None:
            self.continued = True

        def abort(self, _reason: str) -> None:
            self.aborted = True

    denied_route = Route()
    guard.route(
        denied_route,
        type("Request", (), {"url": "https://redirected.example.net/path"})(),
    )
    assert denied_route.aborted is True
    assert denied_route.continued is False

    allowed_route = Route()
    guard.route(
        allowed_route,
        type("Request", (), {"url": "https://cdn.example.com/script.js"})(),
    )
    assert allowed_route.continued is True
    assert allowed_route.aborted is False


def test_pc_worker_rejects_missing_or_oversized_policy_snapshot() -> None:
    job: dict[str, Any] = {
        "job_id": 1,
        "owner_user_id": 1,
        "session_id": 1,
        "profile_key": "owner-1-session-1",
        "resume_url": None,
        "action": "read",
        "arguments": {"selector": ""},
        "lease_seconds": 90,
    }
    with pytest.raises(ValueError, match="network_policy"):
        pc_worker._validate_job(job)

    job["network_policy"] = {
        "version": 1,
        "allow_domains": [f"d{i}.example" for i in range(129)],
        "deny_domains": [],
        "block_all": False,
    }
    with pytest.raises(ValueError, match="allow_domains"):
        pc_worker._validate_job(job)


@pytest.mark.asyncio
async def test_remote_screenshot_can_only_land_in_owner_workspace(tmp_path) -> None:
    payload = {
        "screenshot_base64": base64.b64encode(b"\xff\xd8\xffmock-jpeg").decode(),
        "mime_type": "image/jpeg",
    }
    workspace = ensure_user_workspace(1)
    target = workspace / "browse" / "remote.jpg"
    await _persist_screenshot(1, str(target), payload)
    assert target.read_bytes() == b"\xff\xd8\xffmock-jpeg"
    assert "screenshot_base64" not in payload

    with pytest.raises(ValueError, match="escaped"):
        await _persist_screenshot(
            1,
            str(tmp_path.parent / "outside.jpg"),
            {
                "screenshot_base64": base64.b64encode(b"\xff\xd8\xffx").decode(),
                "mime_type": "image/jpeg",
            },
        )


@pytest.mark.asyncio
async def test_manager_remote_backend_rechecks_domain_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def remote_backend() -> str:
        return "remote"

    async def blocked(_url: str) -> tuple[bool, str, str]:
        return False, "", "blocked by owner policy"

    async def must_not_execute(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("blocked URL reached the remote queue")

    from app.adapters.remote_browser import gateway  # noqa: PLC0415

    monkeypatch.setattr(browser_manager, "browser_backend", remote_backend)
    monkeypatch.setattr(browser_manager, "check_url", blocked)
    monkeypatch.setattr(gateway, "execute", must_not_execute)

    result = await browser_manager.run(
        12,
        "open",
        user_id=7,
        url="https://blocked.example",
    )

    assert result == {"ok": False, "error": "blocked by owner policy"}


@pytest.mark.asyncio
async def test_manager_remote_backend_forwards_owner_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, str, dict[str, Any]]] = []

    async def remote_backend() -> str:
        return "remote"

    async def execute(
        user_id: int,
        session_id: int,
        command: str,
        **arguments: Any,
    ) -> dict[str, Any]:
        calls.append((user_id, session_id, command, arguments))
        return {"ok": True, "text": "ready"}

    from app.adapters.remote_browser import gateway  # noqa: PLC0415

    monkeypatch.setattr(browser_manager, "browser_backend", remote_backend)
    monkeypatch.setattr(gateway, "execute", execute)

    result = await browser_manager.run(
        12,
        "read",
        user_id=7,
        selector="#content",
    )

    assert result == {"ok": True, "text": "ready"}
    assert calls == [(7, 12, "read", {"selector": "#content"})]
