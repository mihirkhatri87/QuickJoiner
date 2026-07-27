"""Microsoft identity + Graph plumbing for the OneDrive / SharePoint connector.

Every other connector authenticates with a *static* secret that `util.resolve_secret`
can read out of an option or an env var. Microsoft 365 can't work that way: the thing
we hold is a **refresh token**, obtained interactively and rotated by Microsoft on
every use. So this module owns three things the rest of the codebase doesn't have:

  1. **Two interactive sign-in flows**, because QuickJoiner runs in two very different
     places. `begin_device_code`/`poll_device_code` is the CLI/Docker/headless path
     (no redirect URI, no client secret — the user types a code at
     microsoft.com/devicelogin). `authorize_url`/`exchange_code` is the web-UI path
     (authorization code + PKCE against a localhost redirect). Both end in the same
     `TokenBundle`.
  2. **A token file per connector** (`<workspace>/oauth/<source>.json`). Deliberately
     the filesystem and not the catalog: `create_connector(source, workspace)` hands a
     connector its name, options and workspace — no catalog — so a file is the only
     store reachable from every path that builds one (CLI, API, scheduler, sync
     manager) without changing that signature. It sits beside `browser_profile/` and
     `uploads/`, which are workspace state for the same reason.
  3. **Delegated access only.** Every scope here is delegated, so the connector can
     see exactly what the signed-in user can see and nothing else — including files
     shared *with* them. There is no application-permission path in this module, and
     that is on purpose: it is what makes a per-user connector safe to hand out.

HONESTY, at rest: the refresh token is stored as plain JSON with the tightest file
permissions the OS gives us, exactly like every other credential in a QuickJoiner
workspace (connector options with literal secrets are stored unencrypted too). It is
never returned by any API response. Encrypting it needs a key-management story —
tracked, not quietly half-built.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import httpx

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"

#: Default tenant. "organizations" accepts any work/school account and rejects
#: personal Microsoft accounts — the right default for *enterprise* OneDrive. A
#: single-tenant app registration should set its tenant id or domain instead.
DEFAULT_TENANT = "organizations"

#: Delegated scopes, composed from the content scopes the connector is configured for.
#: `offline_access` is what makes a refresh token (and therefore background sync)
#: possible at all; `User.Read` is what `test()` uses to name the signed-in account.
BASE_SCOPES = ("offline_access", "User.Read")
SCOPE_FOR_CONTENT = {
    "my_drive": "Files.Read",
    "shared_with_me": "Files.Read.All",
    "sharepoint": "Sites.Read.All",
}

#: Refresh this long before the access token actually expires, so a long-running sync
#: never hands Graph a token that dies mid-flight.
_EXPIRY_SKEW_SECONDS = 300


class GraphError(RuntimeError):
    """A Graph request failed in a way the caller should surface, not retry."""


class GraphAuthError(GraphError):
    """Sign-in is required: no token, or the refresh token is dead (expired, revoked,
    password changed, or a Conditional Access policy kicked in). Deliberately its own
    type so a sync can tell the user to sign in again rather than printing a raw 401 —
    and so `is_transient_network_error` correctly classifies it as *not* worth
    retrying, because waiting will never fix it."""


def scopes_for(content_scopes: Iterable[str]) -> list[str]:
    """Delegated scopes needed for the selected content scopes, least-privilege first.

    Asking only for what was configured matters in an enterprise tenant: `Files.Read`
    is usually user-consentable, while `Sites.Read.All` typically needs an admin. A
    user who only wants their own drive shouldn't be blocked behind that.
    """
    scopes = list(BASE_SCOPES)
    for content in content_scopes:
        scope = SCOPE_FOR_CONTENT.get(content)
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes


# --------------------------------------------------------------------------- PKCE

def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) for RFC 7636 S256. The verifier never leaves this process;
    only its hash goes in the authorize URL, so an intercepted redirect can't be
    replayed for a token."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def authorize_url(
    tenant: str,
    client_id: str,
    redirect_uri: str,
    scopes: Iterable[str],
    state: str,
    challenge: str,
) -> str:
    """The URL to send a browser to for the authorization-code + PKCE flow (pure)."""
    from urllib.parse import urlencode

    query = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Enterprise tenants often have several signed-in accounts in the browser;
        # without this the flow silently picks one and the user can't tell which.
        "prompt": "select_account",
    })
    return f"{AUTHORITY}/{tenant}/oauth2/v2.0/authorize?{query}"


# ------------------------------------------------------------------- token bundle

@dataclass
class TokenBundle:
    access_token: str = ""
    refresh_token: str = ""
    #: ISO-8601 UTC. Empty means "unknown", which is treated as expired.
    expires_at: str = ""
    scopes: list[str] = field(default_factory=list)
    #: userPrincipalName, filled in by `whoami` after sign-in — shown in the UI so a
    #: user can see *which* account a connector is syncing as.
    account: str = ""
    tenant: str = DEFAULT_TENANT
    client_id: str = ""

    def expired(self, now: Optional[datetime] = None) -> bool:
        if not self.expires_at:
            return True
        try:
            deadline = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return (now or datetime.now(timezone.utc)) >= deadline - timedelta(seconds=_EXPIRY_SKEW_SECONDS)

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token, "refresh_token": self.refresh_token,
            "expires_at": self.expires_at, "scopes": self.scopes, "account": self.account,
            "tenant": self.tenant, "client_id": self.client_id,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TokenBundle":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=data.get("expires_at", ""),
            scopes=list(data.get("scopes") or []),
            account=data.get("account", ""),
            tenant=data.get("tenant", DEFAULT_TENANT),
            client_id=data.get("client_id", ""),
        )


def bundle_from_response(payload: dict[str, Any], previous: Optional[TokenBundle] = None) -> TokenBundle:
    """Build a bundle from a token endpoint response (pure).

    Microsoft usually rotates the refresh token on every redemption, but does not
    *always* return a new one — so an absent `refresh_token` keeps the previous one
    rather than blanking it, which would silently turn a working connector into one
    that needs re-authentication on its next sync.
    """
    expires_in = int(payload.get("expires_in") or 0)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    return TokenBundle(
        access_token=payload.get("access_token", ""),
        refresh_token=payload.get("refresh_token") or (previous.refresh_token if previous else ""),
        expires_at=expires_at,
        scopes=(payload.get("scope") or "").split() or (list(previous.scopes) if previous else []),
        account=previous.account if previous else "",
        tenant=previous.tenant if previous else DEFAULT_TENANT,
        client_id=previous.client_id if previous else "",
    )


# ------------------------------------------------------------------- token storage

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def token_path(workspace: Path, source_id: str) -> Path:
    """`<workspace>/oauth/<sanitised source id>.json`.

    A source_id is `type:name`, and `:` is not a legal Windows filename character, so
    it is sanitised; a short hash of the original keeps two names that sanitise to the
    same string from sharing one token file.
    """
    digest = hashlib.sha256(source_id.encode()).hexdigest()[:8]
    return workspace / "oauth" / f"{_SAFE.sub('_', source_id)}-{digest}.json"


def load_token(workspace: Path, source_id: str) -> Optional[TokenBundle]:
    path = token_path(workspace, source_id)
    if not path.exists():
        return None
    try:
        return TokenBundle.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def save_token(workspace: Path, source_id: str, bundle: TokenBundle) -> None:
    path = token_path(workspace, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-replace so an interrupted save can't leave a truncated token file
    # that would read back as "not signed in".
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(bundle.to_json(), indent=2), encoding="utf-8")
    tmp.replace(path)
    _restrict_permissions(path)


def delete_token(workspace: Path, source_id: str) -> bool:
    path = token_path(workspace, source_id)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _restrict_permissions(path: Path) -> None:
    """Owner-only, best effort. chmod is a no-op for ACL purposes on Windows, so this
    is a POSIX hardening step, not a cross-platform guarantee — said plainly rather
    than implied."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------- HTTP helpers

def _client(transport: Any = None, timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=timeout, follow_redirects=True)


def _token_endpoint(tenant: str) -> str:
    return f"{AUTHORITY}/{tenant}/oauth2/v2.0/token"


def _post_token(tenant: str, data: dict[str, str], transport: Any = None) -> dict[str, Any]:
    with _client(transport) as client:
        resp = client.post(_token_endpoint(tenant), data=data)
    payload: dict[str, Any]
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code >= 400:
        raise _auth_error(payload, resp.status_code)
    return payload


def _auth_error(payload: dict[str, Any], status: int) -> GraphError:
    code = payload.get("error", "")
    detail = payload.get("error_description") or f"HTTP {status}"
    # invalid_grant is the family that always means "the user must sign in again":
    # expired/revoked refresh token, changed password, new Conditional Access policy.
    if code in ("invalid_grant", "interaction_required", "consent_required"):
        return GraphAuthError(f"Sign-in required ({code}): {detail.splitlines()[0]}")
    return GraphError(f"{code or 'error'}: {detail.splitlines()[0]}")


# ----------------------------------------------------------------- device code flow

def begin_device_code(
    tenant: str, client_id: str, scopes: Iterable[str], transport: Any = None
) -> dict[str, Any]:
    """Start the device-code flow. Returns Microsoft's payload, whose `message` is
    already a human-readable instruction ("To sign in, use a web browser to open …")."""
    with _client(transport) as client:
        resp = client.post(
            f"{AUTHORITY}/{tenant}/oauth2/v2.0/devicecode",
            data={"client_id": client_id, "scope": " ".join(scopes)},
        )
    payload = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        raise _auth_error(payload, resp.status_code)
    return payload


def poll_device_code(
    tenant: str,
    client_id: str,
    device_code: str,
    interval: int = 5,
    expires_in: int = 900,
    transport: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> TokenBundle:
    """Block until the user completes sign-in in their browser, then return the tokens.

    `authorization_pending` is the expected state for most of this loop, not an error;
    `slow_down` means Microsoft wants a longer interval and must be honoured or it
    starts rejecting outright. `sleep`/`now` are injected so tests don't wait.
    """
    deadline = now() + expires_in
    wait = max(1, interval)
    while True:
        if now() >= deadline:
            raise GraphAuthError("Sign-in timed out — the device code expired. Start again.")
        sleep(wait)
        with _client(transport) as client:
            resp = client.post(_token_endpoint(tenant), data={
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            })
        payload = resp.json() if resp.content else {}
        if resp.status_code < 400:
            return bundle_from_response(payload, TokenBundle(tenant=tenant, client_id=client_id))
        error = payload.get("error", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            wait += 5
            continue
        if error == "expired_token":
            raise GraphAuthError("Sign-in timed out — the device code expired. Start again.")
        raise _auth_error(payload, resp.status_code)


# --------------------------------------------------------- authorization code + PKCE

def exchange_code(
    tenant: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    scopes: Iterable[str],
    transport: Any = None,
) -> TokenBundle:
    payload = _post_token(tenant, {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": " ".join(scopes),
    }, transport=transport)
    return bundle_from_response(payload, TokenBundle(tenant=tenant, client_id=client_id))


def refresh_token(bundle: TokenBundle, scopes: Iterable[str], transport: Any = None) -> TokenBundle:
    if not bundle.refresh_token:
        raise GraphAuthError("No refresh token stored — sign in again.")
    payload = _post_token(bundle.tenant, {
        "client_id": bundle.client_id,
        "grant_type": "refresh_token",
        "refresh_token": bundle.refresh_token,
        "scope": " ".join(scopes),
    }, transport=transport)
    refreshed = bundle_from_response(payload, bundle)
    refreshed.account = bundle.account
    return refreshed


# ------------------------------------------------------------------- Graph client

class GraphClient:
    """Authenticated Microsoft Graph calls for one connector.

    Owns the access-token lifecycle: refreshes on demand and **persists** the result,
    because Microsoft rotates refresh tokens — dropping the rotated one on the floor
    would work for the rest of the process and then fail on the next sync.
    """

    def __init__(
        self,
        workspace: Path,
        source_id: str,
        scopes: Iterable[str],
        transport: Any = None,
        timeout: float = 60.0,
    ):
        self.workspace = workspace
        self.source_id = source_id
        self.scopes = list(scopes)
        self._transport = transport
        self._timeout = timeout
        self._bundle: Optional[TokenBundle] = None

    # -- tokens ------------------------------------------------------------
    def bundle(self) -> TokenBundle:
        if self._bundle is None:
            loaded = load_token(self.workspace, self.source_id)
            if loaded is None:
                raise GraphAuthError(
                    "Not signed in to Microsoft 365 for this connector. Run "
                    f"`qj onedrive login {self.source_id.split(':', 1)[-1]}` or use "
                    "Sign in with Microsoft in Settings."
                )
            self._bundle = loaded
        return self._bundle

    def access_token(self) -> str:
        bundle = self.bundle()
        if bundle.expired():
            bundle = refresh_token(bundle, self.scopes, transport=self._transport)
            self._bundle = bundle
            save_token(self.workspace, self.source_id, bundle)
        return bundle.access_token

    def signed_in(self) -> bool:
        return load_token(self.workspace, self.source_id) is not None

    # -- requests ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token()}", "Accept": "application/json"}

    def get(self, url: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """GET a Graph resource. `url` may be absolute (a nextLink/deltaLink, which
        already carries its own query) or a path relative to the Graph root."""
        full = url if url.startswith("http") else f"{GRAPH}{url}"
        resp = self._request("GET", full, params=params)
        return resp.json() if resp.content else {}

    def post(self, url: str, body: Any) -> dict[str, Any]:
        full = url if url.startswith("http") else f"{GRAPH}{url}"
        resp = self._request("POST", full, json=body)
        return resp.json() if resp.content else {}

    def download(self, drive_id: str, item_id: str, max_bytes: int) -> Optional[bytes]:
        """Item content, or None when it is larger than `max_bytes`.

        The size check happens on the *response*, not just the item metadata, because
        Graph's `size` facet can lag for freshly-changed files.
        """
        url = f"{GRAPH}/drives/{drive_id}/items/{item_id}/content"
        resp = self._request("GET", url)
        content = resp.content
        return None if len(content) > max_bytes else content

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One Graph call with throttle-aware retry.

        Graph throttles aggressively and tells you exactly how long to wait via
        `Retry-After`; ignoring it is the reliable way to get throttled harder. 401 is
        retried **once** after a forced refresh, because an access token can be revoked
        mid-sync (a policy change) even though it has not expired by the clock.
        """
        attempts = 4
        refreshed = False
        for attempt in range(attempts):
            last = attempt == attempts - 1
            with _client(self._transport, self._timeout) as client:
                resp = client.request(method, url, headers=self._headers(), **kwargs)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                bundle = self.bundle()
                self._bundle = refresh_token(bundle, self.scopes, transport=self._transport)
                save_token(self.workspace, self.source_id, self._bundle)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and not last:
                time.sleep(_retry_after_seconds(resp.headers.get("retry-after"), attempt))
                continue
            if resp.status_code == 401:
                raise GraphAuthError(
                    "Microsoft 365 rejected the stored credentials (401). Sign in again."
                )
            if resp.status_code >= 400:
                raise GraphError(_graph_message(resp))
            return resp
        raise GraphError("Microsoft Graph did not respond successfully")  # unreachable

    # -- convenience -------------------------------------------------------
    def whoami(self) -> dict[str, Any]:
        return self.get("/me")


def _retry_after_seconds(header: Optional[str], attempt: int) -> float:
    """Honour Retry-After when it is a sane number of seconds, else exponential
    backoff. Capped so one hostile header can't park a sync for an hour."""
    backoff = 1.0 * (2 ** attempt)
    if header:
        try:
            return min(30.0, max(backoff, float(header.strip())))
        except ValueError:
            pass
    return backoff


def _graph_message(resp: httpx.Response) -> str:
    """Graph's error envelope is `{"error": {"code": ..., "message": ...}}`; surfacing
    that beats surfacing a bare status code."""
    try:
        error = resp.json().get("error", {})
        code = error.get("code", "")
        message = error.get("message", "")
        if code or message:
            return f"Microsoft Graph {resp.status_code} {code}: {message}".strip()
    except ValueError:
        pass
    return f"Microsoft Graph returned HTTP {resp.status_code}"
