"""Role-based access control for QuickJoiner's own API.

The control plane (`agent/control.py`'s `qj_api` tool, and — defense in depth — the HTTP
routes themselves via `api/app.py`'s `_require`) consults this module to decide whether the
acting user may perform a given `(method, path)` call.

Three layers of gate, all here:
  1. **Capability** — the user's role must grant the capability a route requires (`ROLE_CAPS`).
  2. **Connector scope** — a route that names a specific connector additionally requires the
     source be visible (read) or manageable (write) by the user; that check lives with the
     caller (it needs the SourceConfig), but this module flags WHICH routes are connector-scoped.
  3. **Danger confirmation** — `connectors:delete` / `memory:reset` need a typed confirm on top.

Pure and dependency-free so it can be imported anywhere (the tool layer, the HTTP layer, and
tests) without a cycle. Open mode (no users) treats everyone as admin — exactly the pre-auth
behaviour, since auth is opt-in (see auth.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

ROLES = ("admin", "editor", "viewer")
DEFAULT_ROLE = "viewer"
OPEN_MODE_ROLE = "admin"  # no users configured => everyone is effectively admin

# Every capability the API surface is partitioned into (OpenAPI tag group × verb).
CAPS = frozenset({
    "status:read",
    "connectors:read", "connectors:write", "connectors:delete",
    "sync:run",
    "memory:write", "memory:reset",
    "search:read", "graph:read",
    "gaps:read", "gaps:write",
    "sessions:read", "sessions:write",
    "settings:read", "settings:write",
    "briefs:write",
    "scrape:run",
    "chat:use",
    "users:admin",
})

# Routes anyone may call regardless of role (login/logout/status/health). Distinct from an
# UNKNOWN route, which is denied — `required_capability` returns None only for unknown routes.
PUBLIC = "public"

# Capabilities that additionally require a typed confirm at the tool layer (§ safety posture).
DANGER_CAPS = frozenset({"connectors:delete", "memory:reset"})

MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

_VIEWER_CAPS = frozenset({
    "status:read", "connectors:read", "search:read", "graph:read",
    "gaps:read", "sessions:read", "settings:read", "chat:use",
})
_EDITOR_CAPS = _VIEWER_CAPS | frozenset({
    "connectors:write", "connectors:delete", "sync:run", "memory:write",
    "gaps:write", "sessions:write", "briefs:write", "scrape:run",
})
_ADMIN_CAPS = CAPS  # everything

ROLE_CAPS: dict[str, frozenset[str]] = {
    "viewer": _VIEWER_CAPS,
    "editor": _EDITOR_CAPS,
    "admin": _ADMIN_CAPS,
}


def capabilities_for(role: str | None) -> frozenset[str]:
    """The capability set for a role; an unknown/None role gets the least privilege."""
    return ROLE_CAPS.get(role or DEFAULT_ROLE, ROLE_CAPS[DEFAULT_ROLE])


@dataclass(frozen=True)
class RouteCap:
    capability: str  # a CAPS member, or PUBLIC
    scope: Optional[str]  # None | "connector_read" | "connector_write"
    connector: Optional[str]  # the connector/source name captured from the path, if scoped


# (METHOD, path template, capability, connector-param name or None). The path template uses
# {param} placeholders; the {conn_param}, when named, is the connector the scope applies to.
# Keep in lockstep with the routes in api/app.py — the route-coverage test enforces it.
_ROUTES: list[tuple[str, str, str, Optional[str]]] = [
    # Authentication / status (public or admin)
    ("GET", "/api/auth/status", PUBLIC, None),
    ("POST", "/api/auth/login", PUBLIC, None),
    ("POST", "/api/auth/logout", PUBLIC, None),
    ("POST", "/api/auth/users", "users:admin", None),      # first-user bootstrap handled by caller
    ("GET", "/api/auth/users", "users:admin", None),
    ("PATCH", "/api/auth/users/{username}", "users:admin", None),
    ("GET", "/api/status", "status:read", None),
    ("GET", "/api/notifications", "status:read", None),
    ("GET", "/api/syncs", "connectors:read", None),
    # Connectors
    ("GET", "/api/connectors/types", "connectors:read", None),
    ("GET", "/api/connectors", "connectors:read", None),
    ("GET", "/api/sources", "connectors:read", None),
    ("POST", "/api/connectors", "connectors:write", None),
    ("PATCH", "/api/connectors/{name}", "connectors:write", "name"),
    ("POST", "/api/connectors/{name}/test", "connectors:write", "name"),
    ("POST", "/api/connectors/{name}/cleanup", "sync:run", "name"),
    ("DELETE", "/api/connectors/{name}", "connectors:delete", "name"),
    # Sync & ingestion
    ("POST", "/api/sync/{source_name}", "sync:run", "source_name"),
    ("POST", "/api/sync/{source_name}/stop", "sync:run", "source_name"),
    ("POST", "/api/sync/{source_name}/pause", "sync:run", "source_name"),
    ("POST", "/api/sync/{source_name}/resume", "sync:run", "source_name"),
    ("GET", "/api/sync/{source_name}/logs", "connectors:read", "source_name"),
    ("POST", "/api/memory/reset", "memory:reset", None),
    ("POST", "/api/learn", "memory:write", None),
    # Ask & search
    ("GET", "/api/search", "search:read", None),
    ("GET", "/api/suggest", "search:read", None),
    ("GET", "/api/documents/{doc_id}/file", "search:read", None),
    ("POST", "/api/scrape", "scrape:run", None),
    ("POST", "/api/chat", "chat:use", None),
    # Knowledge graph
    ("GET", "/api/graph", "graph:read", None),
    ("GET", "/api/graph/path", "graph:read", None),
    ("GET", "/api/graph/search", "graph:read", None),
    ("GET", "/api/graph/bridges", "graph:read", None),
    # Knowledge gaps
    ("GET", "/api/gaps", "gaps:read", None),
    ("POST", "/api/gaps/resolve", "gaps:write", None),
    # Settings
    ("GET", "/api/settings", "settings:read", None),
    ("GET", "/api/settings/defaults", "settings:read", None),
    ("PATCH", "/api/settings", "settings:write", None),
    ("POST", "/api/llm/test", "settings:write", None),
    # Briefs & repo docs
    ("GET", "/api/briefs", "search:read", None),
    ("POST", "/api/briefs/{brief_type}", "briefs:write", None),
    ("POST", "/api/repos/{source_name}/agents-md", "briefs:write", "source_name"),
    # Sessions & projects
    ("GET", "/api/projects", "sessions:read", None),
    ("POST", "/api/projects", "sessions:write", None),
    ("GET", "/api/sessions", "sessions:read", None),
    ("GET", "/api/sessions/{session_id}", "sessions:read", None),
    ("POST", "/api/sessions/{session_id}/distill", "sessions:write", None),
    ("DELETE", "/api/sessions/{session_id}", "sessions:write", None),
    ("DELETE", "/api/sessions", "sessions:write", None),
]


def _compile(template: str) -> re.Pattern[str]:
    # {param} -> a named group matching one path segment.
    pattern = re.sub(r"\{(\w+)\}", lambda m: f"(?P<{m.group(1)}>[^/]+)", template)
    return re.compile(f"^{pattern}$")


_COMPILED: list[tuple[str, re.Pattern[str], str, Optional[str], str]] = [
    (
        method,
        _compile(template),
        capability,
        ("connector_write" if method in MUTATING_METHODS else "connector_read") if conn else None,
        conn or "",
    )
    for method, template, capability, conn in _ROUTES
]


def required_capability(method: str, path: str) -> Optional[RouteCap]:
    """Resolve a live `(method, path)` to the capability + connector scope it needs.

    Returns None for an unknown route (the caller must then DENY — an unmapped endpoint is
    never silently allowed). `path` should be the URL path without the query string.
    """
    method = method.upper()
    path = path.split("?", 1)[0].rstrip("/") or "/"
    for m, rx, capability, scope, conn_param in _COMPILED:
        if m != method:
            continue
        match = rx.match(path)
        if match:
            connector = match.group(conn_param) if scope and conn_param else None
            return RouteCap(capability=capability, scope=scope, connector=connector)
    return None


def can(role: str | None, method: str, path: str) -> bool:
    """Does `role` grant the CAPABILITY a `(method, path)` needs? (Connector scope and danger
    confirmation are separate, caller-enforced gates — this is only the capability layer.)
    An unknown route is denied."""
    rc = required_capability(method, path)
    if rc is None:
        return False
    if rc.capability == PUBLIC:
        return True
    return rc.capability in capabilities_for(role)


def is_danger(capability: str) -> bool:
    return capability in DANGER_CAPS
