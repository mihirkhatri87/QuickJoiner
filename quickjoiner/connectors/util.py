"""Shared helpers for connectors: secret resolution and HTTP JSON calls."""

from __future__ import annotations

import os
from typing import Any

import httpx


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


def get_json(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = 60.0,
) -> Any:
    resp = httpx.get(url, headers=headers, params=params, auth=auth, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def post_json(
    url: str,
    body: Any,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = 60.0,
) -> Any:
    resp = httpx.post(url, json=body, headers=headers, params=params, auth=auth, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
