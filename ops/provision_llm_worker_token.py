"""Provision an LLM worker token without exposing it in shell arguments/output.

This helper is intentionally called only by the explicit PowerShell
``-ProvisionToken`` flow. It rotates the server-side token through
``worker_queue.rotate_worker_token`` and atomically updates the repository
``.env`` while preserving every unrelated line. The previous file is kept in
``.env.persona-worker.bak``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from app.adapters.remote_browser.credentials import rotate_browser_worker_token
from app.llm.worker_queue import rotate_worker_token
from app.settings import get_settings
from app.storage.db import init_database

_TOKEN_KEY = "PERSONA_WORKER_TOKEN"  # noqa: S105
_BROWSER_TOKEN_KEY = "PERSONA_BROWSER_WORKER_TOKEN"  # noqa: S105
_BACKUP_NAME = ".env.persona-worker.bak"


def _replace_token_lines(text: str, tokens: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    output: list[str] = []
    remaining = dict(tokens)
    for line in lines:
        stripped = line.lstrip()
        matched = next(
            (key for key in tokens if stripped.startswith(f"{key}=")),
            None,
        )
        if matched is not None:
            if matched in remaining:
                output.append(f"{matched}={remaining.pop(matched)}{newline}")
            continue
        output.append(line)
    if remaining:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += newline
        output.extend(f"{key}={value}{newline}" for key, value in remaining.items())
    return "".join(output)


def _replace_token_line(text: str, token: str) -> str:
    """Backward-compatible single-token helper used by older callers/tests."""
    return _replace_token_lines(text, {_TOKEN_KEY: token})


def _atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode if mode is not None else 0o600)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def update_env_token(env_path: Path, token: str) -> Path | None:
    """Atomically upsert token and return backup path when an old file existed."""
    original = ""
    original_mode: int | None = None
    backup_path: Path | None = None
    if env_path.exists():
        original = env_path.read_text(encoding="utf-8")
        original_mode = env_path.stat().st_mode
        backup_path = env_path.with_name(_BACKUP_NAME)
        backup_temp = backup_path.with_suffix(f"{backup_path.suffix}.tmp")
        shutil.copy2(env_path, backup_temp)
        os.replace(backup_temp, backup_path)
    updated = _replace_token_line(original, token)
    _atomic_write(env_path, updated, original_mode)
    return backup_path


def update_env_tokens(
    env_path: Path,
    llm_token: str,
    browser_token: str,
) -> Path | None:
    """Atomically upsert independent LLM and browser worker credentials."""
    original = ""
    original_mode: int | None = None
    backup_path: Path | None = None
    if env_path.exists():
        original = env_path.read_text(encoding="utf-8")
        original_mode = env_path.stat().st_mode
        backup_path = env_path.with_name(_BACKUP_NAME)
        backup_temp = backup_path.with_suffix(f"{backup_path.suffix}.tmp")
        shutil.copy2(env_path, backup_temp)
        os.replace(backup_temp, backup_path)
    updated = _replace_token_lines(
        original,
        {
            _TOKEN_KEY: llm_token,
            _BROWSER_TOKEN_KEY: browser_token,
        },
    )
    _atomic_write(env_path, updated, original_mode)
    return backup_path


async def provision(repo_root: Path) -> Path | None:
    """Rotate server token, then write it to ``repo_root/.env`` atomically."""
    previous_cwd = Path.cwd()
    try:
        os.chdir(repo_root)
        # Existing production databases must not replay all migrations merely
        # to rotate a token. Fresh installs still need the base schema.
        if not get_settings().db_path.exists():
            await init_database()
        token = await rotate_worker_token()
        browser_token = await rotate_browser_worker_token()
        try:
            return update_env_tokens(repo_root / ".env", token, browser_token)
        finally:
            # Best-effort lifetime reduction; Python strings cannot be securely wiped.
            token = ""
            browser_token = ""
    finally:
        os.chdir(previous_cwd)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision Persona LLM worker token")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    backup = asyncio.run(provision(args.repo.resolve()))
    print("Persona LLM worker token provisioned securely.")
    if backup is not None:
        print(f"Previous .env preserved at {backup.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
