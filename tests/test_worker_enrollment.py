from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.adapters.remote_browser.credentials import (
    rotate_browser_worker_token,
    validate_browser_worker_token,
)
from app.adapters.worker_enrollment import SqliteWorkerEnrollment
from app.application.worker_enrollment import (
    EnrollmentCredentials,
    EnrollmentError,
    EnrollmentIssue,
    EnrollmentTicket,
    WorkerEnrollmentService,
)
from app.llm.worker_queue import rotate_worker_token, validate_worker_token
from app.web.routes import automation_settings, worker_enrollment

if TYPE_CHECKING:
    import aiosqlite

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
WORKER_ID = "persona-pc-aabbccddeeff001122334455"


async def _owner(db: aiosqlite.Connection) -> int:
    existing = await (
        await db.execute(
            "SELECT id FROM users WHERE email=?",
            ("owner@example.test",),
        )
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    cursor = await db.execute(
        "INSERT INTO users(email, password_hash) VALUES(?, ?)",
        ("owner@example.test", "x"),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


async def _ticket(
    db: aiosqlite.Connection,
    *,
    expected_worker_id: str | None = None,
) -> tuple[WorkerEnrollmentService, EnrollmentTicket]:
    owner_id = await _owner(db)
    service = WorkerEnrollmentService(SqliteWorkerEnrollment())
    issued = await service.issue(
        EnrollmentIssue(
            owner_user_id=owner_id,
            is_primary_owner=True,
            expected_worker_id=expected_worker_id,
        ),
        now=NOW,
    )
    return service, issued


def test_enrollment_adds_exactly_one_public_route() -> None:
    assert [
        (route.path, route.methods)
        for route in worker_enrollment.router.routes
    ] == [
        ("/api/llm/worker/enrollment", {"POST"}),
    ]


def test_ip_limit_short_circuits_without_consuming_global_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def reject_ip(key: str, _maximum: int, _window: int) -> bool:
        calls.append(key)
        return False

    monkeypatch.setattr(worker_enrollment.rate_limit, "allow", reject_ip)
    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "server": ("persona.example", 443),
            "client": ("203.0.113.8", 1234),
            "path": "/api/llm/worker/enrollment",
            "headers": [],
        }
    )

    assert worker_enrollment._public_rate_allowed(request) is False
    assert calls == ["worker-enrollment:public:ip:203.0.113.8"]


@pytest.mark.asyncio
async def test_exchange_is_pending_then_activation_rotates_both_credentials(
    db: aiosqlite.Connection,
) -> None:
    old_llm = await rotate_worker_token()
    old_browser = await rotate_browser_worker_token()
    service, issued = await _ticket(db)

    credentials = await service.exchange(
        issued.ticket,
        WORKER_ID,
        now=NOW + timedelta(minutes=4),
    )

    assert credentials.llm_worker_token != credentials.browser_worker_token
    assert credentials.activation_expires_at == NOW + timedelta(
        hours=24,
        minutes=4,
    )
    assert await validate_worker_token(old_llm)
    assert await validate_browser_worker_token(old_browser)
    assert not await validate_worker_token(credentials.llm_worker_token)
    assert not await validate_browser_worker_token(
        credentials.browser_worker_token,
    )
    assert credentials.llm_worker_token not in repr(credentials)
    assert credentials.browser_worker_token not in repr(credentials)
    pending_status = await service.status(now=NOW + timedelta(hours=1))
    assert pending_status["pending_activations"] == 1
    assert pending_status["activated_enrollments"] == 0

    activation = await service.activate(
        ledger_id=credentials.ledger_id,
        worker_id=WORKER_ID,
        llm_worker_token=credentials.llm_worker_token,
        browser_worker_token=credentials.browser_worker_token,
        now=NOW + timedelta(hours=3),
    )

    assert activation.already_active is False
    assert await validate_worker_token(credentials.llm_worker_token)
    assert await validate_browser_worker_token(
        credentials.browser_worker_token,
    )
    assert not await validate_worker_token(old_llm)
    assert not await validate_browser_worker_token(old_browser)
    activated_status = await service.status(now=NOW + timedelta(hours=3))
    assert activated_status["pending_activations"] == 0
    assert activated_status["activated_enrollments"] == 1

    recovered = await service.activate(
        ledger_id=credentials.ledger_id,
        worker_id=WORKER_ID,
        llm_worker_token=credentials.llm_worker_token,
        browser_worker_token=credentials.browser_worker_token,
        now=NOW + timedelta(days=2),
    )
    assert recovered.already_active is True
    assert recovered.activated_at == activation.activated_at


@pytest.mark.asyncio
async def test_pending_activation_expires_without_rotating_active_tokens(
    db: aiosqlite.Connection,
) -> None:
    old_llm = await rotate_worker_token()
    old_browser = await rotate_browser_worker_token()
    service, issued = await _ticket(db)
    credentials = await service.exchange(issued.ticket, WORKER_ID, now=NOW)

    with pytest.raises(EnrollmentError, match="expired"):
        await service.activate(
            ledger_id=credentials.ledger_id,
            worker_id=WORKER_ID,
            llm_worker_token=credentials.llm_worker_token,
            browser_worker_token=credentials.browser_worker_token,
            now=NOW + timedelta(hours=25),
        )

    assert await validate_worker_token(old_llm)
    assert await validate_browser_worker_token(old_browser)


@pytest.mark.asyncio
async def test_wrong_activation_credentials_do_not_rotate_kv(
    db: aiosqlite.Connection,
) -> None:
    old_llm = await rotate_worker_token()
    old_browser = await rotate_browser_worker_token()
    service, issued = await _ticket(db)
    credentials = await service.exchange(issued.ticket, WORKER_ID, now=NOW)

    with pytest.raises(EnrollmentError, match="invalid_credentials"):
        await service.activate(
            ledger_id=credentials.ledger_id,
            worker_id=WORKER_ID,
            llm_worker_token="x" * 40,
            browser_worker_token=credentials.browser_worker_token,
            now=NOW,
        )

    assert await validate_worker_token(old_llm)
    assert await validate_browser_worker_token(old_browser)


@pytest.mark.asyncio
async def test_replay_ticket_expiry_and_worker_binding(
    db: aiosqlite.Connection,
) -> None:
    service, issued = await _ticket(db, expected_worker_id=WORKER_ID)
    with pytest.raises(EnrollmentError, match="worker_mismatch"):
        await service.exchange(issued.ticket, "persona-pc-other", now=NOW)
    await service.exchange(issued.ticket, WORKER_ID, now=NOW)
    with pytest.raises(EnrollmentError, match="replayed"):
        await service.exchange(issued.ticket, WORKER_ID, now=NOW)

    service, expiring = await _ticket(db)
    with pytest.raises(EnrollmentError, match="expired"):
        await service.exchange(
            expiring.ticket,
            WORKER_ID,
            now=NOW + timedelta(minutes=6),
        )


@pytest.mark.asyncio
async def test_concurrent_exchange_has_exactly_one_winner(
    db: aiosqlite.Connection,
) -> None:
    service, issued = await _ticket(db)

    async def exchange() -> EnrollmentCredentials | EnrollmentError:
        try:
            return await service.exchange(issued.ticket, WORKER_ID, now=NOW)
        except EnrollmentError as exc:
            return exc

    outcomes = await asyncio.gather(exchange(), exchange())
    winners = [item for item in outcomes if isinstance(item, EnrollmentCredentials)]
    failures = [item for item in outcomes if isinstance(item, EnrollmentError)]
    assert len(winners) == 1
    assert len(failures) == 1
    assert failures[0].reason == "replayed"


@pytest.mark.asyncio
async def test_new_issue_revokes_previous_pending_activation(
    db: aiosqlite.Connection,
) -> None:
    owner_id = await _owner(db)
    service = WorkerEnrollmentService(SqliteWorkerEnrollment())
    first = await service.issue(
        EnrollmentIssue(owner_id, is_primary_owner=True),
        now=NOW,
    )
    pending = await service.exchange(first.ticket, WORKER_ID, now=NOW)
    second = await service.issue(
        EnrollmentIssue(owner_id, is_primary_owner=True),
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(EnrollmentError, match="not_pending"):
        await service.activate(
            ledger_id=pending.ledger_id,
            worker_id=WORKER_ID,
            llm_worker_token=pending.llm_worker_token,
            browser_worker_token=pending.browser_worker_token,
            now=NOW + timedelta(seconds=2),
        )
    assert await service.exchange(
        second.ticket,
        WORKER_ID,
        now=NOW + timedelta(seconds=2),
    )


@pytest.mark.asyncio
async def test_rate_limited_public_call_never_reaches_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    runtime_config = AsyncMock()
    monkeypatch.setattr(worker_enrollment, "_service", service)
    monkeypatch.setattr(
        worker_enrollment.worker_queue,
        "worker_runtime_config",
        runtime_config,
    )
    monkeypatch.setattr(worker_enrollment, "_public_rate_allowed", lambda _request: False)
    app = FastAPI()
    app.include_router(worker_enrollment.router)

    bodies = (
        {"phase": "exchange", "ticket": "pe1_" + "a" * 43, "worker_id": WORKER_ID},
        {
            "phase": "activate",
            "enrollment_id": 1,
            "worker_id": WORKER_ID,
            "llm_worker_token": "a" * 40,
            "browser_worker_token": "b" * 40,
        },
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://public.example",
    ) as client:
        responses = [
            await client.post("/api/llm/worker/enrollment", json=body)
            for body in bodies
        ]

    assert [response.status_code for response in responses] == [429, 429]
    assert all(response.headers["retry-after"] == "60" for response in responses)
    service.exchange.assert_not_awaited()
    service.activate.assert_not_awaited()
    runtime_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_runtime_config_is_rejected_before_ticket_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    monkeypatch.setattr(worker_enrollment, "_service", service)
    monkeypatch.setattr(worker_enrollment, "_public_rate_allowed", lambda _request: True)
    monkeypatch.setattr(
        worker_enrollment.worker_queue,
        "worker_runtime_config",
        AsyncMock(return_value={"chat_model": "", "embedding_model": "embed"}),
    )
    app = FastAPI()
    app.include_router(worker_enrollment.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://persona.example",
    ) as client:
        with pytest.raises(
            RuntimeError,
            match="runtime model configuration",
        ):
            await client.post(
                "/api/llm/worker/enrollment",
                json={
                    "phase": "exchange",
                    "ticket": "pe1_" + "a" * 43,
                    "worker_id": WORKER_ID,
                },
            )

    service.exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_issue_exchange_activation_audit_has_no_plaintext(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = await _owner(db)
    session = {
        "token": "session",
        "user_id": owner_id,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "email": "owner@example.test",
        "display_name": "Owner",
    }
    app = FastAPI()
    app.include_router(automation_settings.router)
    app.include_router(worker_enrollment.router)
    app.dependency_overrides[automation_settings.current_user_required] = lambda: session
    monkeypatch.setattr(worker_enrollment, "is_primary_owner", AsyncMock(return_value=True))
    monkeypatch.setattr(worker_enrollment, "_public_rate_allowed", lambda _request: True)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        issued_response = await client.post(
            "/settings/automation",
            headers={"Origin": "https://testserver"},
            json={"action": "issue_worker_enrollment"},
        )
        assert issued_response.status_code == 200, issued_response.text
        ticket = issued_response.json()["ticket"]
        exchanged = await client.post(
            "/api/llm/worker/enrollment",
            json={"phase": "exchange", "ticket": ticket, "worker_id": WORKER_ID},
        )
        assert exchanged.status_code == 200, exchanged.text
        pending = exchanged.json()
        activated = await client.post(
            "/api/llm/worker/enrollment",
            json={
                "phase": "activate",
                "enrollment_id": pending["enrollment_id"],
                "worker_id": WORKER_ID,
                "llm_worker_token": pending["llm_worker_token"],
                "browser_worker_token": pending["browser_worker_token"],
            },
        )
        retry = await client.post(
            "/api/llm/worker/enrollment",
            json={
                "phase": "activate",
                "enrollment_id": pending["enrollment_id"],
                "worker_id": WORKER_ID,
                "llm_worker_token": pending["llm_worker_token"],
                "browser_worker_token": pending["browser_worker_token"],
            },
        )

    assert activated.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["already_active"] is True
    rows = await (
        await db.execute(
            """
            SELECT action, actor, target, detail, success
              FROM audit_log
             WHERE action LIKE 'worker.enrollment.%'
             ORDER BY id
            """
        )
    ).fetchall()
    assert [row["action"] for row in rows] == [
        "worker.enrollment.issue",
        "worker.enrollment.exchange",
        "worker.enrollment.activate",
        "worker.enrollment.activate",
    ]
    durable = "\n".join(
        str(value or "")
        for row in rows
        for value in (row["actor"], row["target"], row["detail"])
    )
    assert ticket not in durable
    assert pending["llm_worker_token"] not in durable
    assert pending["browser_worker_token"] not in durable


@pytest.mark.asyncio
async def test_owner_issue_requires_json_and_same_origin(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = await _owner(db)
    app = FastAPI()
    app.include_router(automation_settings.router)
    app.dependency_overrides[automation_settings.current_user_required] = lambda: {
        "user_id": owner_id,
    }
    monkeypatch.setattr(worker_enrollment, "is_primary_owner", AsyncMock(return_value=True))

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.10", 1234)),
        base_url="https://persona.example",
    ) as client:
        wrong_origin = await client.post(
            "/settings/automation",
            headers={"Origin": "https://evil.example"},
            json={"action": "issue_worker_enrollment"},
        )
        wrong_type = await client.post(
            "/settings/automation",
            headers={
                "Origin": "https://persona.example",
                "Content-Type": "text/plain",
            },
            content='{"action":"issue_worker_enrollment"}',
        )

    assert wrong_origin.status_code == 403
    assert wrong_type.status_code == 415


@pytest.mark.asyncio
async def test_trusted_proxy_forwarded_https_is_honored(
    monkeypatch: pytest.MonkeyPatch,
    db: aiosqlite.Connection,
) -> None:
    _ = db
    inner = FastAPI()
    inner.include_router(worker_enrollment.router)
    app = ProxyHeadersMiddleware(inner, trusted_hosts=["192.168.33.3"])
    monkeypatch.setattr(worker_enrollment, "_public_rate_allowed", lambda _request: True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.168.33.3", 4321)),
        base_url="http://persona.example",
    ) as client:
        response = await client.post(
            "/api/llm/worker/enrollment",
            headers={"X-Forwarded-Proto": "https"},
            json={
                "phase": "exchange",
                "ticket": "pe1_" + "a" * 43,
                "worker_id": WORKER_ID,
            },
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_deleting_owner_cascades_enrollment_ledger(
    db: aiosqlite.Connection,
) -> None:
    _, issued = await _ticket(db)
    await db.execute("PRAGMA foreign_keys = ON")
    owner_row = await (
        await db.execute(
            "SELECT owner_user_id FROM worker_enrollment_ticket WHERE id=?",
            (issued.ledger_id,),
        )
    ).fetchone()
    assert owner_row is not None
    await db.execute(
        "DELETE FROM users WHERE id=?",
        (int(owner_row["owner_user_id"]),),
    )
    await db.commit()
    row = await (
        await db.execute(
            "SELECT id FROM worker_enrollment_ticket WHERE id=?",
            (issued.ledger_id,),
        )
    ).fetchone()
    assert row is None
