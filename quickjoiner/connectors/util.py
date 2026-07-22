"""Shared helpers for connectors: secret resolution and HTTP JSON calls."""

from __future__ import annotations

import os
import socket
import time
from typing import Any

import httpx


def as_bool(value: Any, default: bool = True) -> bool:
    """Coerce a form/option value (bool or string) to bool. Form option values arrive
    as strings, so "false"/"0"/"no"/"off"/"" (any case) read as False; None -> default."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def resolve_secret(options: dict[str, Any], key: str, env_var: str | None = None) -> str | None:
    """Resolve a credential from options; 'env:NAME' values and a fallback env var are supported.

    Precedence: options[key] (literal or 'env:NAME' indirection) -> os.environ[env_var].
    """
    value = options.get(key)
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:])
    if value:
        return str(value)
    if env_var:
        return os.environ.get(env_var)
    return None


# Transient failures worth retrying, so a single network blip / gateway hiccup during a
# long multi-thousand-call sync doesn't kill the whole run (as a WinError 10060 timeout
# did to a 3.3-hour TFS sync). Network errors (connect/read timeouts, resets) + 429/5xx.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 1.0  # seconds; exponential: 1, 2, 4

# Winsock error codes that mean "the network, not the request, is the problem":
# 10060 timed-out, 10054 reset, 10061 refused, 10064 host-down, 10065 unreachable,
# 11001/11002 DNS lookup failed. (WinError 10060 is the blip that killed the 3.3h TFS sync.)
_NETWORK_WINERRORS = frozenset({10050, 10051, 10054, 10060, 10061, 10064, 10065, 11001, 11002})
# POSIX errnos for the same conditions (ECONNRESET/ETIMEDOUT/ECONNREFUSED/ENETUNREACH/EHOSTUNREACH).
_NETWORK_ERRNOS = frozenset({101, 104, 110, 111, 113})


def is_transient_network_error(exc: BaseException) -> bool:
    """True when `exc` is a *transient network* failure worth waiting out and retrying —
    as opposed to a request/auth/config error that would fail identically no matter how
    long we wait. This is the classifier the sync manager uses to decide whether a sync
    that died mid-pull should enter auto-retry (network) or fail immediately (everything
    else). Covers httpx transport errors, a persisted 429/5xx (`HTTPStatusError` after the
    HTTP layer's own retries were exhausted), and raw socket/OS-level connection failures
    (incl. Windows `WinError 10060` timeouts) from connectors that don't route through
    `get_json`/`post_json`."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUSES
    if isinstance(exc, (httpx.TransportError, socket.timeout, socket.gaierror,
                        ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, OSError):
        return (getattr(exc, "winerror", None) in _NETWORK_WINERRORS
                or getattr(exc, "errno", None) in _NETWORK_ERRNOS)
    return False


def _request_with_retry(method: str, url: str, **kwargs: Any) -> Any:
    for attempt in range(_MAX_ATTEMPTS):
        last = attempt == _MAX_ATTEMPTS - 1
        try:
            resp = httpx.request(method, url, **kwargs)
        except httpx.TransportError:  # timeouts, connection resets, DNS, etc.
            if last:
                raise
            time.sleep(_BACKOFF_BASE * (2 ** attempt))
            continue
        if resp.status_code in _RETRY_STATUSES and not last:
            time.sleep(_BACKOFF_BASE * (2 ** attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")  # loop always returns or raises


def get_json(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = 60.0,
    verify: bool = True,
) -> Any:
    return _request_with_retry(
        "GET", url, headers=headers, params=params, auth=auth, timeout=timeout,
        follow_redirects=True, verify=verify,
    )


def post_json(
    url: str,
    body: Any,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = 60.0,
    verify: bool = True,
) -> Any:
    return _request_with_retry(
        "POST", url, json=body, headers=headers, params=params, auth=auth,
        timeout=timeout, verify=verify,
    )
