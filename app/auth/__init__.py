"""Authentication helpers — passwords, sessions, current-user dependency."""

from app.auth.dependency import (
    current_user_optional,
    current_user_required,
)
from app.auth.passwords import (
    hash_password,
    verify_password,
)
from app.auth.sessions import (
    SESSION_COOKIE_NAME,
    count_active_non_owner_sessions,
    issue_session,
    revoke_non_owner_sessions,
    revoke_session,
    verify_session,
)
from app.auth.users import (
    authenticate,
    count_users,
    create_user,
    normalise_email,
    validate_password,
)

__all__ = [
    "SESSION_COOKIE_NAME",
    "authenticate",
    "count_active_non_owner_sessions",
    "count_users",
    "create_user",
    "current_user_optional",
    "current_user_required",
    "hash_password",
    "issue_session",
    "normalise_email",
    "revoke_non_owner_sessions",
    "revoke_session",
    "validate_password",
    "verify_password",
    "verify_session",
]
