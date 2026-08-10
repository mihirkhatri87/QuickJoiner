"""Per-user secrets for scoped skills, layered over the server's own environment.

A skill that reaches a real system needs credentials, and in a multi-user workspace those
are not one shared value — each person acts as themselves. So a skill's environment is
composed at call time from three layers, **highest wins**:

    1. the calling user's own secrets      (set by them, visible to nobody else)
    2. workspace-wide secrets              (set by an admin: shared endpoints, tenant ids)
    3. the server process's own environment

which is what lets a common `OCTOPUS_URL` sit at workspace level while each person
supplies their own `OCTOPUS_API_KEY`, and lets a user override a shared default without
an admin.

**Encrypted at rest**, unlike connector options (which are stored plaintext and merely
masked on read). The difference is deliberate: these are individual people's own
credentials, and a workspace database gets copied — for a backup, for a bug report, to
try something against real data. A copied `catalog.db` on its own yields nothing here,
because the key lives in a separate file that such copies don't carry.

Honest about what that does and doesn't buy: anyone who can read BOTH the database and
`secrets.key` can decrypt, and the server itself must be able to, so this is protection
against a leaked copy — not against someone with the machine.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

KEY_FILE = "secrets.key"

# Values are read into a subprocess environment; a runaway size would be a memory issue
# rather than a security one, but bounding it keeps a paste accident from becoming one.
MAX_VALUE_CHARS = 8000
WORKSPACE_OWNER = ""  # the sentinel owner for a workspace-wide value


class SecretsUnavailable(RuntimeError):
    """Raised when secrets cannot be read or written — never swallowed into "no secrets",
    because that would silently downgrade a scoped skill to running with the wrong (or
    the server's own) credentials."""


def _fernet(workspace: Path):
    """The workspace's encryption key, created on first use with owner-only permissions.

    Imported lazily so a deployment that never touches skills doesn't need the dependency
    resolved at import time.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise SecretsUnavailable(
            "the 'cryptography' package is required to store skill secrets") from exc

    path = workspace / KEY_FILE
    try:
        if not path.exists():
            workspace.mkdir(parents=True, exist_ok=True)
            path.write_bytes(Fernet.generate_key())
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600; a no-op on some filesystems
            except OSError:
                pass
        return Fernet(path.read_bytes().strip())
    except OSError as exc:
        raise SecretsUnavailable(f"could not access {path}: {exc}") from exc
    except Exception as exc:  # malformed/rotated key
        raise SecretsUnavailable(f"the secrets key in {path} is unusable: {exc}") from exc


class SecretStore:
    """Reads and writes the layered secret store. Holds no plaintext beyond a call."""

    def __init__(self, catalog, workspace: Path):
        self._catalog = catalog
        self._workspace = Path(workspace)
        self._cipher = None

    def _cipher_or_raise(self):
        if self._cipher is None:
            self._cipher = _fernet(self._workspace)
        return self._cipher

    # -- writes ---------------------------------------------------------------

    def set(self, owner: str, key: str, value: str) -> None:
        """Store one value. `owner` is a username, or WORKSPACE_OWNER for a shared one."""
        if not key or not key.strip():
            raise ValueError("a secret needs a name")
        if len(value or "") > MAX_VALUE_CHARS:
            raise ValueError(f"value is longer than {MAX_VALUE_CHARS} characters")
        token = self._cipher_or_raise().encrypt((value or "").encode("utf-8")).decode("ascii")
        self._catalog.set_skill_secret(owner, key.strip(), token)

    def delete(self, owner: str, key: str) -> None:
        self._catalog.delete_skill_secret(owner, key)

    # -- reads ----------------------------------------------------------------

    def keys(self, owner: str) -> list[str]:
        """Which names this owner has set. Never returns values — the UI shows presence,
        not content, and there is no endpoint that reads a secret back out."""
        return self._catalog.skill_secret_keys(owner)

    def _decrypt(self, token: str) -> str | None:
        try:
            return self._cipher_or_raise().decrypt(token.encode("ascii")).decode("utf-8")
        except SecretsUnavailable:
            raise
        except Exception:
            # A value encrypted under a key that has since been replaced. Dropping it is
            # right — the alternative is running a skill with a corrupt credential — and
            # `missing` in resolve() then reports it as absent, which it effectively is.
            return None

    def resolve(self, user: str | None, names: list[str],
                include_process_env: bool = True) -> tuple[dict[str, str], list[str]]:
        """(values, missing) for the environment variables `names`, layered user over
        workspace over the process environment.

        `missing` is what could not be filled from any layer — returned rather than
        silently omitted, so a caller can refuse to run and say exactly which credential
        the person still needs to supply.

        `include_process_env=False` drops the bottom layer, and is what a **user-scoped**
        skill uses: a credential the SERVER holds is not this person's, so falling back to
        it would run the skill as somebody else while looking like success. Better to
        report the value as missing and let them supply their own.
        """
        wanted = [n for n in dict.fromkeys(names) if n]
        if not wanted:
            return {}, []
        owners = [WORKSPACE_OWNER] + ([user] if user else [])
        try:
            stored = self._catalog.skill_secrets_for(owners)
        except Exception as exc:
            raise SecretsUnavailable(f"could not read stored secrets: {exc}") from exc

        values: dict[str, str] = {}
        missing: list[str] = []
        for name in wanted:
            # Lowest layer first, each overwriting the last — process env, then workspace,
            # then the user's own.
            found = os.environ.get(name) if include_process_env else None
            for owner in owners:
                token = stored.get(owner, {}).get(name)
                if token:
                    plain = self._decrypt(token)
                    if plain is not None:
                        found = plain
            if found is None:
                missing.append(name)
            else:
                values[name] = found
        return values, missing
