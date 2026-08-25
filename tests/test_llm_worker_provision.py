"""Security/atomicity tests for the explicit LLM worker token provisioner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ops import provision_llm_worker_token as provisioner

if TYPE_CHECKING:
    import pytest


def test_worker_token_backup_is_gitignored() -> None:
    """Бэкап токена воркера не должен попадать в git.

    Раньше тест искал в .gitignore буквальную строку ``.env.persona-worker.bak``.
    Правило заменили на общее ``.env.*`` (перечисление по именам пропускало
    новые файлы вроде ``.env.prod``), и тест упал, хотя защита стала шире.
    Спрашиваем git напрямую: важно, ИГНОРИРУЕТСЯ ли файл, а не какой строкой.
    """
    import subprocess  # noqa: PLC0415 — нужен только здесь

    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "-q", ".env.persona-worker.bak"],  # noqa: S607
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, (
        ".env.persona-worker.bak НЕ игнорируется git — бэкап токена воркера "
        "может уехать в репозиторий."
    )


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

    async def fake_rotate_browser_worker_token() -> str:
        return "browser-" + secret

    monkeypatch.setattr(provisioner, "init_database", fake_init_database)
    monkeypatch.setattr(provisioner, "rotate_worker_token", fake_rotate_worker_token)
    monkeypatch.setattr(
        provisioner,
        "rotate_browser_worker_token",
        fake_rotate_browser_worker_token,
    )

    await provisioner.provision(tmp_path)

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"PERSONA_WORKER_TOKEN={secret}" in env_text
    assert f"PERSONA_BROWSER_WORKER_TOKEN=browser-{secret}" in env_text


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

    async def fake_rotate_browser_worker_token() -> str:
        return "new-browser-secret"

    monkeypatch.setattr(
        provisioner,
        "get_settings",
        lambda: SimpleNamespace(db_path=db_path),
    )
    monkeypatch.setattr(provisioner, "init_database", unexpected_init_database)
    monkeypatch.setattr(provisioner, "rotate_worker_token", fake_rotate_worker_token)
    monkeypatch.setattr(
        provisioner,
        "rotate_browser_worker_token",
        fake_rotate_browser_worker_token,
    )

    await provisioner.provision(tmp_path)

    assert "PERSONA_WORKER_TOKEN=new-secret" in (
        tmp_path / ".env"
    ).read_text(encoding="utf-8")
    assert "PERSONA_BROWSER_WORKER_TOKEN=new-browser-secret" in (
        tmp_path / ".env"
    ).read_text(encoding="utf-8")
