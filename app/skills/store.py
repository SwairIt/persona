"""T29 — installable skills.

A "skill" is a named instruction set the user installs from a GitHub repo
(``SKILL.md`` or, failing that, ``README.md``). Enabled skills are
injected into the chat system prompt so the model follows them. Only TEXT
is fetched and stored — no code from the repo is ever executed — so
installing a skill from an arbitrary repo cannot run anything on the box.
"""

from __future__ import annotations

import re

import httpx

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.skills")

_MAX_SKILL_BYTES = 60_000
_PER_SKILL_PROMPT_CHARS = 8_000


def _parse_github(url: str) -> tuple[str, str] | None:
    """``https://github.com/user/repo[/...]`` → ``(user, repo)``."""
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url.strip())
    if not match:
        return None
    return match.group(1), match.group(2).removesuffix(".git")


def _extract_name(text: str, fallback: str) -> str:
    """Skill name: frontmatter ``name:`` → first ``# h1`` → repo name."""
    fm = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
    if fm:
        return fm.group(1).strip().strip("\"'")[:60] or fallback
    h1 = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if h1:
        return h1.group(1).strip()[:60] or fallback
    return fallback


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code == 200 and r.text.strip():
        return r.text[:_MAX_SKILL_BYTES]
    return None


async def fetch_skill_from_github(url: str) -> tuple[str, str, str]:
    """Download a skill's instructions. Returns ``(name, content, raw_url)``.

    Raises :class:`ValueError` with a human message on any failure.
    """
    parsed = _parse_github(url)
    if not parsed:
        raise ValueError("это не похоже на ссылку вида github.com/пользователь/репозиторий")
    user, repo = parsed
    candidates = [
        f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{fn}"
        for branch in ("main", "master")
        for fn in ("SKILL.md", "skill.md", "README.md", "readme.md")
    ]
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for raw_url in candidates:
            text = await _fetch_text(client, raw_url)
            if text:
                return _extract_name(text, repo), text, raw_url
    raise ValueError(f"не нашёл SKILL.md или README.md в {user}/{repo}")


async def save_skill(user_id: int, name: str, content: str, source_url: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO skill (user_id, name, content, source_url, enabled) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(user_id, name) DO UPDATE SET "
            "content = excluded.content, source_url = excluded.source_url, enabled = 1",
            (user_id, name, content, source_url),
        )
        await conn.commit()


async def set_skill_enabled(user_id: int, skill_id: int, enabled: bool) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE skill SET enabled = ? WHERE id = ? AND user_id = ?",
            (1 if enabled else 0, skill_id, user_id),
        )
        await conn.commit()


async def list_skills(user_id: int) -> list[dict[str, object]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, name, source_url, enabled, created_at "
            "FROM skill WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def enabled_skills_prompt(user_id: int) -> str:
    """System-prompt fragment listing the user's enabled skills."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT name, content FROM skill WHERE user_id = ? AND enabled = 1 ORDER BY id",
            (user_id,),
        )
        rows = await cur.fetchall()
    if not rows:
        return ""
    parts = [
        "",
        "── Установленные навыки (skills) ──",
        "Это инструкции, которые пользователь добавил. Применяй их, когда уместно:",
    ]
    for r in rows:
        parts.append(f"\n### Навык: {r['name']}\n{str(r['content'])[:_PER_SKILL_PROMPT_CHARS]}")
    return "\n".join(parts)
