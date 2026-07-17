"""Shared helpers for connectors: secret resolution and HTTP JSON calls."""

from __future__ import annotations

import os
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
