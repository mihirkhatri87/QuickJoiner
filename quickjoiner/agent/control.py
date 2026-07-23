"""Control-plane agent tools — `/qj` natural-language control of QuickJoiner via its OWN API.

The user's chosen design (plan 08): one generic `qj_api(method, path, body)` tool dispatches
against QuickJoiner's real HTTP API in-process (Starlette TestClient over the same FastAPI app
and AppContext — no network, no duplicated handler logic), and one `qj_api_reference` tool lists
the endpoints the caller is allowed to use plus the connector field schema. Every call is a
single `(method, path)` choke point, so RBAC (quickjoiner/rbac.py) gates it in ONE place.

Three gates, all enforced here before anything runs (defense in depth — the HTTP layer also
gets `_require` in stage 3):
  1. capability — the acting role must grant what the route needs;
  2. connector scope — a route naming a connector needs it visible (read) / manageable (write);
  3. danger confirm — `connectors:delete` / `memory:reset` need an explicit typed `confirm=true`
     on top of the conversational confirmation the prompt already requires for any mutation.

The acting user is injected into the in-process request via a per-process internal secret header
(see api/app.py's `_internal_user` middleware), so the dispatched handler runs AS that user
without needing their bearer token — and a network client can never forge it (it doesn't know
the per-process secret).
"""

from __future__ import annotations

import json
from typing import Any

from quickjoiner import rbac
from quickjoiner.auth import Auth, can_manage, visible
from quickjoiner.llm.base import AgentTool, ToolSpec

# Streaming endpoints don't fit a request/response tool — don't dispatch them.
_SSE_HINTS = ("/api/chat", "/api/scrape")
_MAX_BODY_CHARS = 20_000


def _ensure_app(ctx) -> Any:
    """The FastAPI app to dispatch against. In the server it was stashed by create_app; in the
    CLI there's no running server, so build one around the SAME AppContext (no re-pull of state,
    and revive=False so a `qj ask` never revives paused syncs as a side effect)."""
    if getattr(ctx, "app", None) is None:
        from quickjoiner.api.app import create_app

        create_app(ctx.workspace, ctx=ctx, revive=False)
    return ctx.app


def _is_sse(path: str) -> bool:
    p = path.split("?", 1)[0]
    return p in _SSE_HINTS or (p.startswith("/api/sync/") and p.endswith("/logs"))


def _dispatch(ctx, user: str | None, method: str, path: str, body: Any) -> tuple[int, str]:
    from starlette.testclient import TestClient

    app = _ensure_app(ctx)
    headers = {
        "X-QJ-Internal-User": user or "",
        "X-QJ-Internal-Auth": getattr(ctx, "internal_secret", "") or "",
    }
    client = TestClient(app)
    resp = client.request(method, path, json=body if body else None, headers=headers)
    return resp.status_code, resp.text


def build_control_tools(ctx, user: str | None, role: str | None) -> list[AgentTool]:
    role = role or Auth(ctx.catalog).role_of(user)
    caps = rbac.capabilities_for(role)

    def _scope_ok(rc: rbac.RouteCap) -> tuple[bool, str]:
        """Connector-scope check: a route naming a connector needs it visible (read) or
        manageable (write) by the acting user. The control connector itself is commons/visible."""
        if not rc.scope or not rc.connector:
            return True, ""
        auth = Auth(ctx.catalog)
        source = next((s for s in ctx.config.sources if s.name == rc.connector), None)
        if source is None:
            return False, f"no connector named {rc.connector!r}"
        if rc.scope == "connector_write":
            if not can_manage(source, user, auth.enabled):
                return False, f"you cannot manage the connector {rc.connector!r}"
        elif not visible(source, user, auth.enabled):
            return False, f"the connector {rc.connector!r} is not visible to you"
        return True, ""

    def qj_api(method: str, path: str, body: dict | None = None, confirm: bool = False) -> str:
        method = str(method).upper().strip()
        path = str(path).strip()
        if not path.startswith("/"):
            path = "/" + path
        rc = rbac.required_capability(method, path)
        if rc is None:
            return (f"DENIED: unknown endpoint {method} {path}. Call qj_api_reference to see the "
                    "endpoints you can use.")
        if rc.capability != rbac.PUBLIC and rc.capability not in caps:
            return (f"PERMISSION DENIED: your role '{role}' lacks the '{rc.capability}' capability "
                    f"for {method} {path}. Tell the user this needs a higher role (an admin can "
                    "grant it) — do not retry.")
        ok, why = _scope_ok(rc)
        if not ok:
            return f"PERMISSION DENIED: {why}. Do not retry."
        if _is_sse(path):
            return (f"{method} {path} is a streaming endpoint and can't be called this way. "
                    "For chat, just answer normally; for a scrape use the /scrape flow; for sync "
                    "logs, tell the user to open the sync's log in the UI.")
        if path.split("?", 1)[0] == "/api/uploads":
            return ("POST /api/uploads takes an uploaded file (multipart) and can't be called this "
                    "way. If the user gave a readable server file path, call POST /api/uploads/local "
                    'with {"path": "<path>"} to ingest it into the rolling Uploads connector; '
                    "otherwise tell them to drag the file onto the chat or use the attach button.")
        if rbac.is_danger(rc.capability) and not confirm:
            return (f"CONFIRM REQUIRED: {method} {path} is destructive ({rc.capability}). State "
                    "exactly what will happen, get the user's explicit yes, THEN re-call with "
                    "confirm=true. Never pass confirm=true without that yes.")
        try:
            status, text = _dispatch(ctx, user, method, path, body)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR dispatching {method} {path}: {exc}"
        text = text[:_MAX_BODY_CHARS] + ("… (truncated)" if len(text) > _MAX_BODY_CHARS else "")
        verdict = "OK" if status < 400 else "FAILED"
        return f"HTTP {status} ({verdict}) for {method} {path}\n{text}"

    def qj_api_reference(area: str = "") -> str:
        app = _ensure_app(ctx)
        try:
            spec = app.openapi()
        except Exception as exc:  # noqa: BLE001
            return f"Could not read the API reference: {exc}"
        area = (area or "").lower().strip()
        lines: list[str] = [
            f"QuickJoiner API endpoints you (role '{role}') may call. Use qj_api(method, path, "
            "body). Secrets go in options as env:VAR_NAME, never literal values."
        ]
        rows: list[tuple[str, str, str]] = []
        for path, methods in sorted(spec.get("paths", {}).items()):
            for method, op in methods.items():
                m = method.upper()
                if m not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
                    continue
                rc = rbac.required_capability(m, path)
                if rc is None or (rc.capability != rbac.PUBLIC and rc.capability not in caps):
                    continue
                summary = op.get("summary", "")
                if area and area not in (path.lower() + " " + " ".join(op.get("tags", [])).lower()):
                    continue
                tag = "danger" if rbac.is_danger(rc.capability) else rc.capability
                rows.append((f"{m} {path}", summary, tag))
        if not rows:
            lines.append("(no endpoints match — your role may be too limited, or refine 'area'.)")
        for call, summary, tag in rows:
            lines.append(f"- {call}  [{tag}]" + (f" — {summary}" if summary else ""))
        # Fold in the connector field schema so "create a connector" is one discovery call.
        if (not area or "connector" in area) and "connectors:read" in caps:
            from quickjoiner.connectors.specs import connector_catalog

            lines.append("\nConnector types & fields (for POST /api/connectors body.options):")
            for spec_c in connector_catalog():
                fields = ", ".join(
                    f"{f['key']}{'*' if f.get('required') else ''}"
                    f"{' (secret→env:VAR)' if f.get('secret') else ''}"
                    for f in spec_c.get("fields", [])
                )
                lines.append(f"- {spec_c['type']}: {fields or '(no fields)'}")
        return "\n".join(lines)

    return [
        AgentTool(
            spec=ToolSpec(
                name="qj_api",
                description=(
                    "Control QuickJoiner itself by calling its OWN API in-process: connectors, "
                    "syncs, settings, gaps, sessions, memory, users/roles. Pass method (GET/POST/"
                    "PATCH/DELETE), path (e.g. /api/connectors), and body. ALWAYS discover first: "
                    "call qj_api_reference to see the endpoints you're allowed to use and the "
                    "connector field schema. For any mutation (POST/PATCH/DELETE) state what you'll "
                    "do and get the user's explicit yes FIRST; destructive calls also need "
                    "confirm=true. Store secrets as env:VAR_NAME, never literal tokens. Your "
                    "permissions are limited by your role — a PERMISSION DENIED result is final."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "method": {"type": "string", "description": "GET | POST | PATCH | DELETE"},
                        "path": {"type": "string", "description": "API path, e.g. /api/connectors"},
                        "body": {"type": "object", "description": "JSON request body (for POST/PATCH)"},
                        "confirm": {"type": "boolean",
                                    "description": "Required true for destructive calls, only after the user agrees"},
                    },
                    "required": ["method", "path"],
                },
            ),
            fn=qj_api,
        ),
        AgentTool(
            spec=ToolSpec(
                name="qj_api_reference",
                description=(
                    "List the QuickJoiner API endpoints the current user is allowed to call (filtered "
                    "by their role) plus the connector types and their fields. Call this BEFORE using "
                    "qj_api so you use the right path/body and know which fields a connector needs. "
                    "Optional 'area' narrows it (e.g. 'connectors', 'sync', 'settings', 'gaps')."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "area": {"type": "string", "description": "Optional filter, e.g. 'connectors'"},
                    },
                },
            ),
            fn=qj_api_reference,
        ),
    ]
