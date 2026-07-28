from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.web.routes import llm_worker


def test_bootstrap_is_ascii_and_contains_no_embedded_worker_token() -> None:
    script = Path("ops/persona_llm_pc_bootstrap.ps1").read_text(encoding="utf-8")

    script.encode("ascii")
    assert "Read-Host" in script
    assert "-AsSecureString" in script
    assert "PERSONA_WORKER_TOKEN=" in script
    assert "New-ScheduledTaskAction" in script
    assert "Start-ScheduledTask" in script
    assert "ollama pull" in script
    assert "/api/llm/worker/agent.py" in script
    assert "github.com" not in script.lower()


@pytest.mark.asyncio
async def test_bootstrap_endpoint_is_no_store() -> None:
    response = await llm_worker.worker_bootstrap_ps1()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert b"PERSONA_WORKER_TOKEN" in response.body


@pytest.mark.asyncio
async def test_probe_validates_token_without_claiming_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_worker = AsyncMock()
    monkeypatch.setattr(llm_worker, "_require_worker", require_worker)

    response = await llm_worker.worker_probe("secret")

    require_worker.assert_awaited_once_with("secret")
    assert response.status_code == 200
    assert response.body == b'{"ok":true}'
