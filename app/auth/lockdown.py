"""Safe operator command for revoking every active non-owner web session.

Usage:
    python -m app.auth.lockdown
    python -m app.auth.lockdown --confirm

The first form is a read-only preview. The confirmed form only stamps
``auth_session.revoked_at``; it never deletes users or owner sessions.
"""

from __future__ import annotations

import argparse
import asyncio

from app.auth.owner import get_owner_user_id
from app.auth.sessions import (
    count_active_non_owner_sessions,
    revoke_non_owner_sessions,
)
from app.settings import get_settings
from app.storage.db import init_database


async def run(*, confirm: bool) -> int:
    # A standalone fresh install needs the schema. Existing deployments must
    # not replay every migration merely to revoke sessions.
    if not get_settings().db_path.exists():
        await init_database()
    owner_user_id = await get_owner_user_id()
    if owner_user_id is None:
        print("error: primary owner is not configured")
        return 1

    active = await count_active_non_owner_sessions(owner_user_id)
    if not confirm:
        print(f"Primary owner user_id: {owner_user_id}")
        print(f"Active non-owner sessions: {active}")
        print("Preview only. Re-run with --confirm to revoke them.")
        return 0

    revoked = await revoke_non_owner_sessions(owner_user_id)
    print(f"Primary owner user_id: {owner_user_id}")
    print(f"Revoked non-owner sessions: {revoked}")
    print("User accounts were not deleted.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Apply revocation. Without this flag the command is read-only.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(confirm=bool(args.confirm)))


if __name__ == "__main__":
    raise SystemExit(main())
