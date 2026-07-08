"""Authentication: local users, PBKDF2 password hashing, bearer tokens.

Auth is OPT-IN. Until the first user is created the workspace runs in "open
mode": no login anywhere, every source visible to everyone (exactly the
pre-auth behavior). Creating the first user enables auth for the workspace.

Sharing model: a source carries `owner` (username or None) and `shared`.
Ownerless sources are commons (visible to all). Owned sources are visible to
their owner, and to everyone else only when shared. Ingested knowledge is a
single communal memory either way — sharing governs who can see and manage a
connector's configuration (and exercise its credentials), not what the org
has already learned.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from quickjoiner.config import SourceConfig
    from quickjoiner.memory.catalog import Catalog

_ITERATIONS = 600_000
SESSION_FILE = ".session"  # <workspace>/.session holds the CLI's bearer token


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS
    ).hex()
    return f"pbkdf2:{_ITERATIONS}:{salt}:{digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split(":")
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    """Auth operations over the workspace catalog."""

    def __init__(self, catalog: "Catalog"):
        self.catalog = catalog

    @property
    def enabled(self) -> bool:
        return self.catalog.count_users() > 0

    def create_user(self, username: str, password: str) -> None:
        username = username.strip()
        if not username or not username.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Usernames use letters, numbers, '-' and '_' only")
        if len(password) < 4:
            raise ValueError("Password must be at least 4 characters")
        if self.catalog.get_user(username):
            raise ValueError(f"User {username!r} already exists")
        self.catalog.create_user(username, hash_password(password))

    def login(self, username: str, password: str) -> str:
        """Verify credentials and issue a bearer token."""
        user = self.catalog.get_user(username)
        if not user or not verify_password(password, user["password_hash"]):
            raise ValueError("Wrong username or password")
        token = new_token()
        self.catalog.save_token(token_hash(token), username)
        return token

    def resolve(self, token: str | None) -> str | None:
        """Token -> username, or None (anonymous / open mode)."""
        if not token:
            return None
        return self.catalog.get_token_user(token_hash(token))

    def logout(self, token: str) -> None:
        self.catalog.delete_token(token_hash(token))


def visible(source: "SourceConfig", user: str | None, auth_enabled: bool) -> bool:
    """Can `user` see (and use) this source?"""
    if not auth_enabled or source.shared or source.owner is None:
        return True
    return source.owner == user


def can_manage(source: "SourceConfig", user: str | None, auth_enabled: bool) -> bool:
    """Can `user` edit, share, or remove this source?"""
    if not auth_enabled or source.owner is None:
        return True
    return source.owner == user
