"""Security/atomicity tests for the explicit LLM worker token provisioner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import provision_llm_worker_token as provisioner


def test_worker_token_backup_is_gitignored() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    rules = (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env.persona-worker.bak" in rules


def test_windows_launcher_recovers_local_ollama() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    script = (repo_root / "ops" / "persona_llm_worker.ps1").read_text(
        encoding="utf-8"
    )
    assert "Start-Process" in script
    assert "-ArgumentList 'serve'" in script
    assert "$ManageLocalOllama" in script
    assert "@('127.0.0.1', 'localhost', '::1')" in script


def test_update_env_token_preserves_content_and_backup(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    original = (
        "# keep this comment\r\n"
        "PERSONA_SERVER=https://persona.example\r\n"
        "PERSONA_WORKER_TOKEN=old-secret\r\n"
        "PERSONA_WORKER_TOKEN=duplicate-old-secret\r\n"
        "OTHER=value\r\n"
    )
    env_path.write_text(original, encoding="utf-8", newline="")

    backup = provisioner.update_env_token(env_path, "new-secret")

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == original.replace("\r\n", "\n")
    updated = env_path.read_text(encoding="utf-8")
    assert "PERSONA_SERVER=https://persona.example" in updated
    assert "OTHER=value" in updated
    assert updated.count("PERSONA_WORKER_TOKEN=") == 1
    assert "PERSONA_WORKER_TOKEN=new-secret" in updated
    assert "old-secret" not in updated


async def test_provision_never_prints_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "must-never-appear-in-output"

    async def fake_init_database() -> None:
        return None

    async def fake_rotate_worker_token() -> str:
        return secret

    monkeypatch.setattr(provisioner, "init_database", fake_init_database)
    monkeypatch.setattr(provisioner, "rotate_worker_token", fake_rotate_worker_token)

    await provisioner.provision(tmp_path)

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert (tmp_path / ".env").read_text(encoding="utf-8").strip() == (
        f"PERSONA_WORKER_TOKEN={secret}"
    )


async def test_provision_does_not_replay_migrations_for_existing_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "data" / "persona.db"
    db_path.parent.mkdir(exist_ok=True)
    db_path.touch()

    async def unexpected_init_database() -> None:
        raise AssertionError("existing database must not replay migrations")

    async def fake_rotate_worker_token() -> str:
        return "new-secret"

    monkeypatch.setattr(
        provisioner,
        "get_settings",
        lambda: SimpleNamespace(db_path=db_path),
    )
    monkeypatch.setattr(provisioner, "init_database", unexpected_init_database)
    monkeypatch.setattr(provisioner, "rotate_worker_token", fake_rotate_worker_token)

    await provisioner.provision(tmp_path)

    assert "PERSONA_WORKER_TOKEN=new-secret" in (
        tmp_path / ".env"
    ).read_text(encoding="utf-8")
