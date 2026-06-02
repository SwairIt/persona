"""Process-name allow/deny lists — skip capture during games or sensitive apps."""

from __future__ import annotations

import json
from pathlib import Path

from app.settings import get_settings

_DEFAULT_DENY: frozenset[str] = frozenset(
    {
        "lsass.exe",
        "winlogon.exe",
        "csrss.exe",
        "smss.exe",
        "1password.exe",
        "bitwarden.exe",
        "keepass.exe",
        "keepassxc.exe",
        "vaultwarden.exe",
        "dashlane.exe",
        "lastpass.exe",
    }
)

_DEFAULT_GAME_DENY: frozenset[str] = frozenset(
    {
        "dota2.exe",
        "csgo.exe",
        "cs2.exe",
        "valorant.exe",
        "valorant-win64-shipping.exe",
        "league of legends.exe",
        "leagueclient.exe",
        "rocketleague.exe",
        "rocketleague-win64-shipping.exe",
        "fortniteclient-win64-shipping.exe",
        "apexlegends.exe",
        "r5apex.exe",
        "rdr2.exe",
        "fivem.exe",
        "minecraft.exe",
        "minecraftlauncher.exe",
        "javaw.exe",
        "factorio.exe",
        "stardew valley.exe",
        "hades.exe",
    }
)


def _whitelist_path() -> Path:
    return get_settings().data_dir / "process_whitelist.json"


def load_user_lists() -> dict[str, list[str]]:
    """Load user-defined allow/deny overrides from JSON, if any."""
    path = _whitelist_path()
    if not path.exists():
        return {"deny": [], "allow_only": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"deny": [], "allow_only": []}
    if not isinstance(payload, dict):
        return {"deny": [], "allow_only": []}
    deny = [str(p).lower() for p in payload.get("deny", []) if isinstance(p, str)]
    allow = [str(p).lower() for p in payload.get("allow_only", []) if isinstance(p, str)]
    return {"deny": deny, "allow_only": allow}


def save_user_lists(deny: list[str], allow_only: list[str]) -> None:
    """Persist user lists to JSON."""
    path = _whitelist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "deny": sorted({p.lower() for p in deny}),
        "allow_only": sorted({p.lower() for p in allow_only}),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def should_capture(process_name: str | None) -> bool:
    """Return True if a screenshot should be saved for this foreground process."""
    if not process_name:
        return True
    lowered = process_name.lower()

    if lowered in _DEFAULT_DENY:
        return False
    if lowered in _DEFAULT_GAME_DENY:
        return False

    user_lists = load_user_lists()
    if lowered in user_lists["deny"]:
        return False
    allow_only = user_lists["allow_only"]
    if allow_only and lowered not in allow_only:
        return False

    return True


def default_deny_list() -> list[str]:
    return sorted(_DEFAULT_DENY | _DEFAULT_GAME_DENY)
