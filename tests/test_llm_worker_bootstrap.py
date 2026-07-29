from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.web.routes import llm_worker
from ops import persona_llm_worker, persona_remote_browser_worker


def _script() -> str:
    return Path("ops/persona_llm_pc_bootstrap.ps1").read_text(encoding="utf-8")


def test_bootstrap_is_ascii_credential_free_and_uses_one_phased_route() -> None:
    script = _script()

    script.encode("ascii")
    assert "[string]$EnrollmentTicket = ''" in script
    assert "$EnrollmentTicket = $null" in script
    assert "Read-Host" in script
    assert "-AsSecureString" in script
    assert script.count('-Uri "$Server/api/llm/worker/enrollment"') == 2
    assert "phase = 'exchange'" in script
    assert "phase = 'activate'" in script
    assert "/enrollment/exchange" not in script
    assert "/enrollment/activate" not in script
    assert "llm_worker_token = $LlmToken" in script
    assert "browser_worker_token = $BrowserToken" in script
    assert "Write-Host $ticket" not in script
    assert "github.com" not in script.lower()


def test_supplied_ticket_overrides_stale_environment_and_is_cleared() -> None:
    script = _script()

    ticket_read = script.index("$ticket = $EnrollmentTicket")
    ticket_clear = script.index("$EnrollmentTicket = $null", ticket_read)
    supplied_branch = script.index(
        "if (-not [string]::IsNullOrWhiteSpace($ticket))",
        ticket_clear,
    )
    supplied_exchange = script.index(
        "Exchange-EnrollmentTicket $ticket $workerId",
        supplied_branch,
    )
    env_fallback = script.index("if ($enrollmentId -eq 0)", supplied_exchange)

    assert ticket_read < ticket_clear < supplied_branch < supplied_exchange
    assert supplied_exchange < env_fallback


def test_pending_env_is_acl_protected_and_saved_before_heavy_work() -> None:
    script = _script()

    atomic_save = script.index(
        "Write-OwnerOnlyAtomic -Path $nextEnvPath",
    )
    pip_install = script.index("& $python -m pip install")
    playwright = script.index("& $python -m playwright install chromium")
    downloads = script.index("Downloading staged worker scripts")
    model_pull = script.index("& $ollama pull $ChatModel")
    promotion = script.index("# Heavy preparation is complete.")
    activation = script.index("$activation = Activate-Enrollment", promotion)

    assert "SetAccessRuleProtection($true, $false)" in script
    assert "Set-Acl -LiteralPath $temporary" in script
    assert "[IO.File]::Replace($temporary, $Path" in script
    assert atomic_save < pip_install < playwright < downloads < model_pull
    assert model_pull < promotion < activation


def test_resume_and_atomic_promotion_preserve_old_runtime_on_heavy_failure() -> None:
    script = _script()

    assert "@($nextEnvPath, $envPath)" in script
    assert "Resuming a durably saved pending enrollment." in script
    assert "PERSONA_ENROLLMENT_ID" in script
    assert "PERSONA_ENROLLMENT_WORKER_ID" in script
    assert "PERSONA_ACTIVATION_EXPIRES_AT" in script
    assert "$workerPyNext = \"$workerPy.next\"" in script
    assert "$browserWorkerPyNext = \"$browserWorkerPy.next\"" in script
    assert "-Source $nextEnvPath" in script
    assert "-Destination $envPath" in script
    assert ".env.persona-worker." in script


def test_launchers_override_stale_process_environment_from_dotenv() -> None:
    script = _script()

    assert "$dotenvLoader = @'" in script
    assert 'Set-Item -Path ("Env:" + $Matches[1]) -Value $Matches[2]' in script
    assert script.count("$dotenvLoader") >= 3
    assert "PERSONA_WORKER_HEARTBEAT_FILE" in script
    assert "PERSONA_BROWSER_HEARTBEAT_FILE" in script


def test_dual_task_install_rolls_back_register_start_and_poll_failures() -> None:
    script = _script()

    function_start = script.index("function Install-PersonaTasksAtomically")
    snapshot = script.index("Export-ScheduledTask", function_start)
    register_llm = script.index("Register-PersonaTask", snapshot)
    register_browser = script.index("Register-PersonaTask", register_llm + 1)
    start_llm = script.index("Start-ScheduledTask -TaskName $TaskName", register_browser)
    start_browser = script.index(
        "Start-ScheduledTask -TaskName $BrowserTaskName",
        start_llm,
    )
    verified = script.index("Wait-WorkerHeartbeats", start_browser)
    rollback = script.index("Register-ScheduledTask `", verified)

    assert snapshot < register_llm < register_browser < start_llm < start_browser
    assert start_browser < verified < rollback
    assert "Unregister-ScheduledTask `" in script
    assert "throw $installError" in script
    assert "did not complete authenticated polls" in script


def test_bootstrap_has_valid_powershell_ast() -> None:
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    path = Path("ops/persona_llm_pc_bootstrap.ps1").resolve()
    command = (
        "$tokens=$null;$errors=$null;"
        f"[Management.Automation.Language.Parser]::ParseFile('{path}',"
        "[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
    runtime_config = AsyncMock(
        return_value={
            "chat_model": "qwen2.5:7b",
            "embedding_model": "nomic-embed-text",
        }
    )
    monkeypatch.setattr(llm_worker, "_require_worker", require_worker)
    monkeypatch.setattr(
        llm_worker.worker_queue,
        "worker_runtime_config",
        runtime_config,
    )

    response = await llm_worker.worker_probe("secret")

    require_worker.assert_awaited_once_with("secret")
    runtime_config.assert_awaited_once_with()
    assert response.status_code == 200


def test_worker_heartbeat_markers_are_atomic(tmp_path: Path) -> None:
    llm_path = tmp_path / "llm.heartbeat"
    browser_path = tmp_path / "browser.heartbeat"

    persona_llm_worker._mark_successful_poll(llm_path)
    persona_remote_browser_worker._mark_successful_poll(browser_path)

    assert llm_path.read_text(encoding="ascii").strip()
    assert browser_path.read_text(encoding="ascii").strip()
    assert not (tmp_path / ".llm.heartbeat.tmp").exists()
    assert not (tmp_path / ".browser.heartbeat.tmp").exists()


def test_telegram_complete_delivery_uses_one_final_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            yield '{"message":{"content":"быстрый "},"done":false}'
            yield '{"message":{"content":"ответ"},"done":true}'

    class FakeOllama:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeOllama:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def stream(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    completed: list[dict[str, object]] = []

    monkeypatch.setattr(persona_llm_worker.httpx, "Client", FakeOllama)
    monkeypatch.setattr(
        persona_llm_worker,
        "_send_chunk",
        lambda *_args, **_kwargs: pytest.fail("unexpected chunk upload"),
    )
    monkeypatch.setattr(
        persona_llm_worker,
        "_send_done",
        lambda _client, _cfg, job_id, **kwargs: completed.append(
            {"job_id": job_id, **kwargs}
        ),
    )

    persona_llm_worker._handle_chat(
        object(),  # type: ignore[arg-type]
        SimpleNamespace(model="qwen2.5:3b", ollama="http://127.0.0.1:11434"),
        {
            "job_id": 9,
            "payload": {
                "messages": [{"role": "user", "content": "привет"}],
                "delivery": "complete",
            },
        },
        SimpleNamespace(stop=False),
    )

    assert completed == [{"job_id": 9, "result": "быстрый ответ"}]


def test_worker_preloads_selected_model_and_reports_rates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "load_duration": 250_000_000,
                "prompt_eval_count": 100,
                "prompt_eval_duration": 500_000_000,
                "eval_count": 20,
                "eval_duration": 1_000_000_000,
            }

    class FakeServer:
        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            response = FakeResponse()
            response.json = lambda: {"chat_model": "qwen2.5:3b"}  # type: ignore[method-assign]
            return response

    payloads: list[dict[str, object]] = []

    class FakeOllama:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeOllama:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(
            self,
            *_args: object,
            json: dict[str, object],
            **_kwargs: object,
        ) -> FakeResponse:
            payloads.append(json)
            return FakeResponse()

    monkeypatch.setattr(persona_llm_worker.httpx, "Client", FakeOllama)
    model = persona_llm_worker._preload_runtime_model(
        FakeServer(),  # type: ignore[arg-type]
        SimpleNamespace(
            server="https://persona.example",
            token="token",
            ollama="http://127.0.0.1:11434",
        ),
    )

    assert model == "qwen2.5:3b"
    assert payloads == [
        {
            "model": "qwen2.5:3b",
            "messages": [],
            "stream": False,
            "think": False,
            "keep_alive": -1,
        }
    ]
    summary = persona_llm_worker._performance_summary(FakeResponse().json())
    assert "load=250ms" in summary
    assert "prompt=100/200.0 tok/s" in summary
    assert "output=20/20.0 tok/s" in summary
