"""FastAPI app: chat (SSE), sources dashboard, sync, search, webhooks."""

from __future__ import annotations

import contextvars
import hmac
import json
import os
import queue
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from quickjoiner.api.hooks import build_hooks_router
from quickjoiner.app import AppContext, build_context
from quickjoiner.auth import Auth, can_manage, visible
from quickjoiner.config import (
    ChatConfig,
    Config,
    EmbeddingConfig,
    GraphConfig,
    LLMConfig,
    ReposConfig,
    RetrievalConfig,
    SourceConfig,
)

STATIC_DIR = Path(__file__).parent / "static"  # legacy vanilla UI (fallback)

# In-process control dispatch (plan 08): the control tools (agent/control.py) call the API
# in-process as the acting user. A request carrying the per-process secret header sets this
# contextvar in the internal-user middleware; `_user` honours it. Network requests never set it
# (they don't know the secret), so it's not a bypass surface. Set within one request's task, so
# the middleware value is visible to that request's handler and reset afterwards.
_internal_user_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qj_internal_user", default=None
)


def _ui_dir() -> Path:
    """Where the web UI lives. Priority: QJ_UI_DIR env -> built React app
    (frontend/dist in a repo checkout, or wherever Docker copied it) -> legacy static."""
    env = os.environ.get("QJ_UI_DIR")
    if env and (Path(env) / "index.html").exists():
        return Path(env)
    react_dist = Path(__file__).parents[2] / "frontend" / "dist"
    if (react_dist / "index.html").exists():
        return react_dist
    return STATIC_DIR

_SENTINEL = object()
MASKED = "•••"


def _locked_option_keys(type_: str) -> set[str]:
    """Option keys marked lock_after_sync in the connector catalog (FORM_SPECS) —
    identity-shaping fields that follow the connector-name rule: editable only while
    the connector has 0 learned documents."""
    from quickjoiner.connectors.specs import FORM_SPECS

    return {f["key"] for f in FORM_SPECS.get(type_, {}).get("fields", [])
            if f.get("lock_after_sync")}


def _norm_opt(value) -> tuple:
    """Comparable form of an option value that may arrive as a list or a comma string
    ('a, b' == ['a','b'] == 'a,b'); None/'' both mean unset."""
    if value is None:
        return ()
    items = value if isinstance(value, list) else str(value).split(",")
    return tuple(sorted(s.strip().lower() for s in items if s and s.strip()))


# --- OpenAPI / Swagger metadata (grouping + top-level description) ------------
_API_DESCRIPTION = """
QuickJoiner is an onboarding-intelligence system: **connectors** pull an org's data (code,
tickets, wikis, CI/CD, logs) into a local vector + graph **memory**, and a grounded agent
answers questions **only from what it has learned**, with citations — or says it hasn't
learned that yet.

### Authentication
The workspace is **open** (no auth) until the first user is created via `POST /api/auth/users`.
After that, sign in with `POST /api/auth/login` and send the token on every write:
`Authorization: Bearer <token>`. Read-only endpoints stay open; management endpoints require a user.

### Streaming endpoints
`POST /api/chat`, `POST /api/scrape`, and `GET /api/sync/{name}/logs` return **Server-Sent
Events** (`text/event-stream`), not JSON — read them line by line (`data: {...}`) until a
terminal `done` event. Every other endpoint is plain JSON.

### A typical end-to-end flow
1. `GET /health` → `GET /api/status`  · 2. (if auth) create user + login  ·
3. `GET /api/connectors/types` → `POST /api/connectors` → `POST /api/connectors/{name}/test`  ·
4. `POST /api/sync/{name}` then poll `GET /api/syncs` (or stream the logs) until done  ·
5. `GET /api/search` / `POST /api/chat` to query  · 6. explore `GET /api/graph`, `GET /api/gaps`.
Ready-to-import **Postman** and **Bruno** collections that walk this flow live in `docs/api/`.
""".strip()

_OPENAPI_TAGS = [
    {"name": "Status", "description": "Liveness + workspace health and memory counts."},
    {"name": "Authentication", "description": "Optional sign-in. Open mode until the first user exists; bearer token thereafter."},
    {"name": "Connectors", "description": "Register, test, edit, clean up, and delete the systems QuickJoiner learns from."},
    {"name": "Sync & ingestion", "description": "Start / pause / resume / stop sync jobs, watch progress and history, and reset all memory."},
    {"name": "Ask & search", "description": "Grounded cited Q&A (SSE), keyword/semantic search, teaching facts, autocomplete, and URL scraping."},
    {"name": "Knowledge graph", "description": "The evidence graph: snapshots, entity search, cross-source bridges, and cited paths between entities."},
    {"name": "Knowledge gaps", "description": "What the org still needs to teach the system — clustered from unanswerable questions."},
    {"name": "Sessions & projects", "description": "Persistent conversations and the projects that scope them."},
    {"name": "Briefs & repo docs", "description": "Generated onboarding briefs, per-repo architecture docs, and citation file views."},
    {"name": "Settings", "description": "Read/update tunable workspace config and test the LLM provider."},
    {"name": "Webhooks", "description": "HMAC-verified push ingestion from connected sources."},
]


class ScopeRequest(BaseModel):
    """Which slice of memory a question may use. All three are OR-ed and resolved
    server-side into (source_ids, doc_ids) before any search runs."""
    source_ids: list[str] = []
    doc_ids: list[str] = []
    tags: list[str] = []


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    project: str | None = None  # project id or name; groups sessions + scopes memory
    provider: str | None = None
    model: str | None = None
    # Per-question context files (from POST /api/chat/attachments). Their text is injected into
    # THIS turn only and their metadata is stamped on the user message — NOT ingested into memory.
    attachment_ids: list[str] = []
    # Narrow this question to chosen connectors/documents/tags. Empty = all of memory
    # (unchanged behaviour). Filters retrieval AND withholds live tools for sources
    # outside the scope, so a scoped question can't spend calls on what you excluded.
    scope: ScopeRequest | None = None


class ProjectRequest(BaseModel):
    name: str
    description: str = ""


class CredentialsRequest(BaseModel):
    username: str
    password: str
    role: str | None = None  # create_user only; ignored on login. First user is always admin.


class RoleUpdate(BaseModel):
    role: str  # admin | editor | viewer


class LearnRequest(BaseModel):
    fact: str
    topic: str | None = None


class UploadLocalRequest(BaseModel):
    path: str  # a file path on the server, ingested into the rolling uploads connector


class BrowserInputEvent(BaseModel):
    """One input event for a remote (headless, polled-screenshot) sign-in session. A fixed
    shape, not an arbitrary passthrough — `connectors/browser/session._apply_input` further
    whitelists `type` and ignores anything it doesn't recognize."""

    type: str  # mousemove | mousedown | mouseup | wheel | keydown | keyup | press | type
    x: float | None = None
    y: float | None = None
    button: str | None = None
    deltaX: float | None = None
    deltaY: float | None = None
    key: str | None = None
    text: str | None = None


def _oauth_page(heading: str, detail: str) -> str:
    """The tiny page Microsoft's redirect lands on. Self-contained (no assets, no JS)
    because it renders in whatever browser did the sign-in, which may not be the one
    QuickJoiner is open in."""
    import html as _html

    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>QuickJoiner · Microsoft 365</title>"
        "<style>body{font:16px/1.6 system-ui,sans-serif;margin:0;display:grid;"
        "place-items:center;min-height:100vh;background:#0f1115;color:#e6e8ee}"
        "div{max-width:32rem;padding:2rem;text-align:center}"
        "h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:0;opacity:.75}</style>"
        f"<div><h1>{_html.escape(heading)}</h1><p>{_html.escape(detail)}</p></div>"
    )


class LabelRequest(BaseModel):
    source_id: str
    #: '' = the whole connector · a folder path = that folder AND anything ingested into it
    #: later · a full document uri = just that document.
    uri_prefix: str = ""
    kind: str = "tag"  # 'tag' (scoping label) | 'aka' (alternate name)
    value: str = ""


class OneDriveLearnRequest(BaseModel):
    #: Shared links (any OneDrive/SharePoint URL the signed-in user can open) and/or
    #: paths inside their own drive. A folder learns the supported files beneath it.
    targets: list[str] = []


class ScrapeRequest(BaseModel):
    url: str
    depth: int = 4
    max_pages: int = 40
    provider: str | None = None
    model: str | None = None


class GapsResolveRequest(BaseModel):
    gap_ids: list[str]
    resolution: str = "dismissed"  # connected:<name> | taught | dismissed


class ConnectorRequest(BaseModel):
    name: str
    type: str
    options: dict = {}
    shared: bool = False
    skip_test: bool = False
    sync_interval_minutes: int | None = None


class ConnectorUpdate(BaseModel):
    name: str | None = None  # rename — only allowed before the first sync (0 documents)
    options: dict | None = None
    shared: bool | None = None
    sync_interval_minutes: int | None = None
    clear_sync_interval: bool = False  # explicit "set to manual" (None is ambiguous over JSON)


class SettingsUpdate(BaseModel):
    org: str | None = None
    llm: dict | None = None
    embedding: dict | None = None
    retrieval: dict | None = None
    chat: dict | None = None
    graph: dict | None = None
    repos: dict | None = None


class LLMTestRequest(BaseModel):
    # Optional overrides to test unsaved provider settings from the form.
    llm: dict | None = None


def _secret_keys(type_: str) -> set[str]:
    from quickjoiner.connectors.specs import FORM_SPECS

    spec = FORM_SPECS.get(type_, {})
    keys = {f["key"] for f in spec.get("fields", []) if f["secret"]}
    return keys or {"token", "api_token", "api_key", "app_key", "password"}


def _mask_options(type_: str, options: dict) -> dict:
    """Hide stored secret values; env: references are safe to show."""
    secret = _secret_keys(type_)
    return {
        k: (v if k not in secret or (isinstance(v, str) and v.startswith("env:")) else MASKED)
        for k, v in options.items()
    }


def _ensure_control_connector(ctx: AppContext, name: str, type_: str) -> None:
    """Seed the permanent control connector as a source if absent (plan 08). Ownerless (commons)
    so everyone sees the plate; it yields no documents, so it never enters memory or the graph.
    Delete/rename/sync of it are refused by the endpoint guards."""
    if any(s.name == name for s in ctx.config.sources):
        return
    source = SourceConfig(name=name, type=type_, options={}, owner=None, shared=True)
    ctx.config.sources.append(source)
    try:
        ctx.catalog.write_source(source)
    except Exception:  # noqa: BLE001 — the in-memory source is enough; persistence is best-effort
        pass


def _ensure_uploads_connector(ctx: AppContext) -> None:
    """Seed the permanent rolling Uploads connector if absent. Ownerless (commons) so everyone
    sees the drop-box plate; unlike the control connector it DOES ingest documents (from the
    upload endpoints and its own folder scan) and is syncable/cleanable — only delete/rename are
    refused by the endpoint guards, so it stays a single, continuously-growing source."""
    from quickjoiner.connectors.uploads import UPLOADS_NAME, UPLOADS_TYPE

    if any(s.name == UPLOADS_NAME for s in ctx.config.sources):
        return
    source = SourceConfig(name=UPLOADS_NAME, type=UPLOADS_TYPE, options={}, owner=None, shared=True)
    ctx.config.sources.append(source)
    try:
        ctx.catalog.write_source(source)
    except Exception:  # noqa: BLE001 — the in-memory source is enough; persistence is best-effort
        pass


def create_app(workspace: Path, ctx: AppContext | None = None, revive: bool = True) -> FastAPI:
    """Build the FastAPI app. `ctx` lets a caller (e.g. the CLI's control tools) reuse an
    existing AppContext instead of building a second one over the same workspace — the app is
    stashed on `ctx.app` for in-process control dispatch. `revive=False` skips re-attaching
    paused syncs (wanted only when actually serving, not when a `qj ask` lazily builds an app)."""
    from quickjoiner.connectors.self_connector import CONTROL_NAME, CONTROL_TYPE

    from quickjoiner.sessions import SessionManager

    from quickjoiner.suggest import QuestionSuggester
    from quickjoiner.sync_manager import SyncManager, _DONE

    if ctx is None:
        ctx = build_context(workspace)
    api = FastAPI(
        title="QuickJoiner API",
        version="0.1.0",
        description=_API_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
    )
    # Wire the app + a per-process internal-dispatch secret onto the ctx so the control tools
    # can call this same app in-process, as the acting user (plan 08).
    ctx.app = api
    if not ctx.internal_secret:
        ctx.internal_secret = secrets.token_urlsafe(32)
    _internal_secret = ctx.internal_secret

    @api.middleware("http")
    async def _internal_user_mw(request, call_next):
        u = request.headers.get("x-qj-internal-user")
        a = request.headers.get("x-qj-internal-auth")
        trusted = bool(u and a and hmac.compare_digest(a, _internal_secret))
        token = _internal_user_var.set(u if trusted else None)
        try:
            return await call_next(request)
        finally:
            _internal_user_var.reset(token)

    _ensure_control_connector(ctx, CONTROL_NAME, CONTROL_TYPE)
    _ensure_uploads_connector(ctx)
    manager = SessionManager(ctx)
    auth = Auth(ctx.catalog)
    suggester = QuestionSuggester(ctx.catalog)
    syncs = SyncManager(ctx)
    # Re-attach any syncs that were paused when a previous process exited (laptop closed /
    # server restarted), so the user can resume them — they re-pull from the watermark.
    if revive:
        syncs.revive_paused()
    api.include_router(build_hooks_router(ctx, syncs))

    def _user(authorization: str | None) -> str | None:
        """Acting username. An in-process control call injects the user via the trusted
        internal-secret header (honoured only in-process); otherwise resolve the bearer token.
        None in open mode or when signed out."""
        internal = _internal_user_var.get()
        if internal is not None:
            return internal
        if authorization and authorization.lower().startswith("bearer "):
            return auth.resolve(authorization[7:].strip())
        return None

    def _require_user(user: str | None) -> None:
        if auth.enabled and user is None:
            raise HTTPException(status_code=401, detail="Sign in to manage connectors")

    def _guard_not_control(name: str, action: str = "changed") -> None:
        """Refuse delete/rename/sync of the permanent control connector (plan 08)."""
        from quickjoiner.connectors.self_connector import is_control_source

        if is_control_source(name=name):
            raise HTTPException(
                status_code=409,
                detail=f"The QuickJoiner control connector is permanent and cannot be {action}.")

    def _guard_not_uploads(name: str, action: str = "changed") -> None:
        """Refuse delete/rename of the permanent rolling Uploads connector (it stays a single,
        continuously-growing source). Sync and clean-up ARE allowed, unlike the control connector."""
        from quickjoiner.connectors.uploads import is_uploads_source

        if is_uploads_source(name=name):
            raise HTTPException(
                status_code=409,
                detail=f"The Uploads connector is permanent and cannot be {action}. "
                       "Drag files onto the chat or POST /api/uploads to add documents to it.")

    def _require(cap: str, user: str | None) -> None:
        """RBAC capability gate at the HTTP layer — defense in depth alongside the control
        tool's own check (plan 08). Open mode (no users) => everyone is admin, so this is a
        no-op until auth is turned on. Placed AFTER existence/visibility checks so a private
        resource still 404s (never leaks) rather than 403-ing for a user who can't see it."""
        from quickjoiner import rbac

        role = auth.role_of(user)
        if cap not in rbac.capabilities_for(role):
            raise HTTPException(
                status_code=403,
                detail=f"Your role '{role}' lacks the '{cap}' capability. An admin can grant it.")

    def _find_source(name: str, user: str | None) -> SourceConfig:
        source = next((s for s in ctx.config.sources if s.name == name), None)
        if source is None or not visible(source, user, auth.enabled):
            raise HTTPException(status_code=404, detail=f"No configured source {name!r}")
        return source

    def _connector_row(source: SourceConfig, user: str | None) -> dict:
        from quickjoiner.connectors.registry import CONNECTOR_TYPES, _load_builtin_connectors
        from quickjoiner.connectors.specs import _MODE_LETTERS

        _load_builtin_connectors()
        cls = CONNECTOR_TYPES.get(source.type)
        modes = [label for flag, label in _MODE_LETTERS if cls and flag in cls.modes]
        source_id = f"{source.type}:{source.name}"
        counts = {s["name"]: s["doc_count"] for s in ctx.catalog.list_sources()}
        return {
            "name": source.name,
            "type": source.type,
            "owner": source.owner,
            "shared": source.shared,
            "mine": source.owner is not None and source.owner == user,
            "can_manage": can_manage(source, user, auth.enabled),
            "documents": counts.get(source.name, 0),
            "last_sync": ctx.catalog.get_sync_state(source_id).get("since"),
            "sync_interval_minutes": source.sync_interval_minutes,
            "options": _mask_options(source.type, source.options),
            "modes": modes,
        }

    # -- auth -----------------------------------------------------------------
    @api.get("/api/auth/status", tags=["Authentication"], summary="Whether sign-in is enabled and who (if anyone) the bearer token identifies, plus that user's RBAC role.")
    def auth_status(authorization: str | None = Header(default=None)):
        user = _user(authorization)
        return {"enabled": auth.enabled, "user": user, "role": auth.role_of(user)}

    @api.post("/api/auth/users", tags=["Authentication"], summary="Create a user. The FIRST user turns authentication ON and is always an admin; afterwards only an admin may add users (and assign a role: admin|editor|viewer, default viewer).")
    def create_user(req: CredentialsRequest, authorization: str | None = Header(default=None)):
        # Bootstrap: anyone may create the FIRST user (open mode). After that, sign-in required
        # AND the creator must be an admin (users:admin) to add more users.
        user = _user(authorization)
        _require_user(user)
        if auth.enabled:
            _require("users:admin", user)
        try:
            role = auth.create_user(req.username, req.password, req.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"username": req.username.strip(), "role": role}

    @api.get("/api/auth/users", tags=["Authentication"], summary="List all users and their RBAC roles (admin only).")
    def list_users(authorization: str | None = Header(default=None)):
        user = _user(authorization)
        _require_user(user)
        _require("users:admin", user)
        return auth.catalog.list_users()

    @api.patch("/api/auth/users/{username}", tags=["Authentication"], summary="Set a user's RBAC role: admin | editor | viewer (admin only).")
    def set_user_role(username: str, req: RoleUpdate, authorization: str | None = Header(default=None)):
        user = _user(authorization)
        _require_user(user)
        _require("users:admin", user)
        try:
            auth.set_role(username, req.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"username": username, "role": req.role}

    @api.post("/api/auth/login", tags=["Authentication"], summary="Sign in with username/password; returns a bearer token to send as `Authorization: Bearer <token>`.")
    def login(req: CredentialsRequest):
        try:
            token = auth.login(req.username, req.password)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        return {"token": token, "username": req.username}

    @api.post("/api/auth/logout", tags=["Authentication"], summary="Revoke the current bearer token.")
    def logout(authorization: str | None = Header(default=None)):
        if authorization and authorization.lower().startswith("bearer "):
            auth.logout(authorization[7:].strip())
        return {"ok": True}

    # -- connector management ---------------------------------------------------
    @api.get("/api/connectors/types", tags=["Connectors"], summary="Catalog of connector types with their configurable fields (labels, required/secret flags) and supported modes — drives the connector forms.")
    def connector_types():
        from quickjoiner.connectors.specs import connector_catalog

        return connector_catalog()

    @api.get("/api/connectors", tags=["Connectors"], summary="List the configured connectors visible to you (ownership/sharing applies when auth is on).")
    def list_connectors(authorization: str | None = Header(default=None)):
        user = _user(authorization)
        return [
            _connector_row(s, user)
            for s in ctx.config.sources
            if visible(s, user, auth.enabled)
        ]

    @api.post("/api/connectors", tags=["Connectors"], summary="Create a connector. Its credentials are tested first; a failing test is reported and the connector is not saved unless forced.")
    def create_connector_endpoint(
        req: ConnectorRequest, authorization: str | None = Header(default=None)
    ):
        from quickjoiner.connectors.registry import create_connector

        user = _user(authorization)
        _require_user(user)
        _require("connectors:write", user)
        from quickjoiner.connectors.uploads import is_uploads_source

        if is_uploads_source(name=req.name, type_=req.type):
            raise HTTPException(
                status_code=409,
                detail="The Uploads connector is a permanent singleton — it already exists. Drag "
                       "files onto the chat or POST /api/uploads to add documents.")
        existing = next((s for s in ctx.config.sources if s.name == req.name), None)
        if existing is not None and not can_manage(existing, user, auth.enabled):
            raise HTTPException(status_code=409, detail=f"Name {req.name!r} is already taken")

        source = SourceConfig(
            name=req.name, type=req.type, options=req.options,
            owner=user, shared=req.shared if auth.enabled else True,
            sync_interval_minutes=req.sync_interval_minutes,
        )
        try:
            connector = create_connector(source, ctx.workspace)
        except ValueError as exc:  # unknown type
            raise HTTPException(status_code=400, detail=str(exc))

        test_result = None
        if not req.skip_test:
            result = connector.test()
            test_result = {"ok": result.ok, "message": result.message}
            if not result.ok:
                raise HTTPException(status_code=422, detail=result.message)

        ctx.config.sources = [s for s in ctx.config.sources if s.name != req.name] + [source]
        ctx.catalog.save_config(ctx.config)  # persists settings + this source (configured=1)
        return {**_connector_row(source, user), "test": test_result}

    @api.patch("/api/connectors/{name}", tags=["Connectors"], summary="Update a connector's options, sharing, or sync schedule. Can also RENAME it, but only before its first sync (0 documents) — the name is the identity that keys all ingested data. Secrets sent back as the mask are preserved; a field cleared to empty is removed.")
    def update_connector(
        name: str, req: ConnectorUpdate, authorization: str | None = Header(default=None)
    ):
        user = _user(authorization)
        _require_user(user)
        _guard_not_control(name, "changed")
        source = _find_source(name, user)
        _require("connectors:write", user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can change this connector")
        # Rename — safe ONLY before the first sync, because the name keys the source_id and
        # thus every document / vector / graph node / watermark / webhook URL. With 0
        # documents nothing is keyed to it yet, so it's just a config move.
        if req.name is not None and req.name.strip() != source.name:
            _guard_not_uploads(name, "renamed")
            new_name = req.name.strip()
            if not new_name:
                raise HTTPException(status_code=400, detail="Name cannot be empty")
            if any(s.name == new_name for s in ctx.config.sources):
                raise HTTPException(status_code=409, detail=f"Name {new_name!r} is already taken")
            old_id = f"{source.type}:{source.name}"
            doc_count = len(ctx.catalog.documents_for_source(old_id))
            if doc_count > 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"Renaming is only allowed before the first sync — this connector has "
                           f"{doc_count} learned document(s). Clean it up first, then rename.")
            if syncs.is_running(source.name):
                raise HTTPException(status_code=409, detail="A sync is running — stop it before renaming")
            ctx.catalog.clear_sync_state(old_id)  # drop the now-orphan watermark (0 docs ⇒ nothing else)
            source.name = new_name  # save_config below writes the new sources row + drops the old one
        if req.shared is not None:
            source.shared = req.shared
        if req.clear_sync_interval:
            source.sync_interval_minutes = None
        elif req.sync_interval_minutes is not None:
            source.sync_interval_minutes = req.sync_interval_minutes
        if req.options is not None:
            # Identity-shaping fields (FORM_SPECS lock_after_sync, e.g. `aka`) follow the
            # same rule as the connector name: editable only before the first sync. After
            # it, edits could not be applied consistently (removing an alias would not
            # un-declare it from the graph) — clean up first, then change them.
            locked_keys = _locked_option_keys(source.type)
            if locked_keys:
                changed = [k for k in locked_keys
                           if k in req.options and req.options[k] != MASKED
                           and _norm_opt(req.options.get(k)) != _norm_opt(source.options.get(k))]
                if changed and ctx.catalog.documents_for_source(f"{source.type}:{source.name}"):
                    raise HTTPException(
                        status_code=409,
                        detail=f"{', '.join(sorted(changed))} can only be changed before the "
                               f"first sync — clean up this connector first, then edit it.")
            merged = dict(source.options)
            for k, v in req.options.items():
                if v == MASKED:
                    continue  # masked placeholder -> keep the stored secret
                if v == "" and k in merged:
                    del merged[k]
                elif v != "":
                    merged[k] = v
            source.options = merged
        ctx.catalog.save_config(ctx.config)
        return _connector_row(source, user)

    @api.post("/api/connectors/{name}/cleanup", tags=["Connectors"], summary="Forget everything this connector taught the system (documents, vectors, graph edges) while KEEPING its config. Runs as a background job.")
    def cleanup_connector(name: str, authorization: str | None = Header(default=None)):
        """Forget everything this connector taught us, keeping its config. Runs as a
        background job (streams to the same log/notification surface as a sync)."""
        user = _user(authorization)
        _require_user(user)
        _guard_not_control(name, "cleaned up")
        source = _find_source(name, user)
        _require("sync:run", user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can clean up this connector")
        try:
            job = syncs.start_cleanup(name, f"{source.type}:{source.name}")
        except RuntimeError as exc:  # a sync is in flight
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job": job.summary()}

    @api.delete("/api/connectors/{name}", tags=["Connectors"], summary="Delete a connector. By default this also purges its learned data (a deleted connector's data is otherwise unreachable); pass keep_memory=true to retain it.")
    def delete_connector(name: str, keep_memory: bool = False,
                         authorization: str | None = Header(default=None)):
        """Remove a connector and, by default, everything it taught us.

        The source_id is `type:name`, so knowledge left behind by a deleted connector is
        unreachable: nothing can re-sync, refresh or purge it, yet it still answers
        questions and occupies the knowledge graph. Cleanup therefore runs automatically
        as a background job. `keep_memory=true` keeps the documents (the old behaviour) —
        for deliberately retiring a source while keeping what it taught."""
        from quickjoiner.connectors.registry import create_connector

        user = _user(authorization)
        _require_user(user)
        _guard_not_control(name, "deleted")
        _guard_not_uploads(name, "deleted")
        source = _find_source(name, user)
        _require("connectors:delete", user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can remove this connector")
        source_id = f"{source.type}:{source.name}"
        if not keep_memory and syncs.is_running(name):
            raise HTTPException(
                status_code=409,
                detail=f"A job is running for {name!r} — stop it before deleting",
            )
        ctx.config.sources = [s for s in ctx.config.sources if s.name != name]
        ctx.catalog.save_config(ctx.config)  # reconciles: removes this source's row
        # Release workspace-side state the source config doesn't carry — OneDrive's
        # stored Microsoft 365 refresh token would otherwise stay redeemable on disk
        # after the connector that owned it is gone. Best effort: never block a delete.
        try:
            create_connector(source, ctx.workspace).on_deleted()
        except Exception:  # noqa: BLE001 - deletion must succeed regardless
            pass
        if keep_memory:
            ctx.catalog.delete_source(source_id)
            return {"removed": name, "job": None}
        # The job drops the catalog row itself, after the documents/vectors/graph are gone.
        job = syncs.start_cleanup(name, source_id)
        return {"removed": name, "job": job.summary()}

    # ------------------------------------------------- credential-gated web scraping
    # Sign-in for a `web_scrape` connector with use_browser=true. Unlike the Microsoft
    # flow below there is no redirect: a real Chromium window opens ON THE SERVER HOST
    # and the user signs in there, so this is a background job the UI polls. The URL is
    # taken from the connector's own configuration, never from the request — this route
    # must not become a way to make the server open an arbitrary page.
    def _scrape_connector(name: str, user: str | None):
        from quickjoiner.connectors.browser.scraper import WebScrapeConnector
        from quickjoiner.connectors.registry import create_connector

        source = _find_source(name, user)
        connector = create_connector(source, ctx.workspace)
        if not isinstance(connector, WebScrapeConnector):
            raise HTTPException(
                status_code=400,
                detail=f"{name!r} is a {source.type} connector, not a web_scrape one",
            )
        starts = connector._start_urls()
        if not starts:
            raise HTTPException(status_code=400, detail=f"{name!r} has no start_urls configured")
        return source, connector, starts[0]

    @api.post("/api/connectors/{name}/browser/login", tags=["Connectors"], summary="Open a sign-in window on the QuickJoiner host for a credential-gated web_scrape connector. Background job — poll GET .../browser/session. 409 if one is already open or the host has no display.")
    def browser_login_start(name: str, authorization: str | None = Header(default=None)):
        """Start an interactive sign-in for a `use_browser=true` scrape connector.

        A real browser window opens **on the machine running QuickJoiner** (same machine as
        the UI in a local-first setup; a headless host is refused with an explanation).
        The window's URL comes from the connector's own `start_urls`."""
        from quickjoiner.connectors.browser import login_jobs

        user = _user(authorization)
        _require_user(user)
        source, connector, url = _scrape_connector(name, user)
        _require("connectors:write", user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can sign this connector in")
        try:
            job = login_jobs.start(ctx.workspace, name, url,
                                   ignore_https_errors=connector._ignore_https_errors())
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"login": job.summary()}

    @api.get("/api/connectors/{name}/browser/session", tags=["Connectors"], summary="Whether this web_scrape connector's saved browser session still works (fetches its start URL and reports content vs sign-in page), plus any in-flight sign-in.")
    def browser_session_status(name: str, verify: bool = True,
                               authorization: str | None = Header(default=None)):
        """Report the saved session's real state. `verify=false` skips the live fetch and
        answers from stored cookies alone — cheap, for polling while a window is open."""
        from quickjoiner.connectors.browser import login_jobs
        from quickjoiner.connectors.browser.session import (
            has_profile,
            session_hosts,
            verify_session,
        )

        user = _user(authorization)
        _source, connector, url = _scrape_connector(name, user)
        job = login_jobs.status(name)
        out: dict = {
            "url": url,
            "has_profile": has_profile(ctx.workspace),
            "hosts": session_hosts(ctx.workspace),
            "can_open_window": login_jobs.display_hint() is None,
            "display_hint": login_jobs.display_hint(),
            "remote_capable": login_jobs.login_mode() == "remote",
            "login": job.summary() if job else None,
        }
        if verify and not (job and job.summary()["active"]):
            ok, detail = verify_session(ctx.workspace, url,
                                        ignore_https_errors=connector._ignore_https_errors())
            out["signed_in"], out["detail"] = ok, detail
        return out

    def _require_scrape_manage(name: str, user: str | None):
        """Shared gate for the remote sign-in stream/input/done routes below: same level as
        starting the login itself (not mere read access), since the stream can show — and the
        input channel can type — credentials for whatever site the connector points at."""
        source, _connector, url = _scrape_connector(name, user)
        _require("connectors:write", user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can control this sign-in")
        return source, _connector, url

    @api.get("/api/connectors/{name}/browser/session/frames", tags=["Connectors"], summary="Server-Sent Events stream of a running remote sign-in's live screencast frames (jpeg, base64), ending with a `done` event. 404 if no remote sign-in is running.")
    def browser_session_frames(name: str, authorization: str | None = Header(default=None)):
        """Live view for a `mode: "remote"` sign-in (headless host, no local display) — the
        counterpart to the real window that opens locally. One `meta` event announces the
        actual frame size first, so the frontend never hardcodes a viewport."""
        from quickjoiner.connectors.browser import login_jobs
        from quickjoiner.connectors.browser.session import _CONTEXT_OPTS

        user = _user(authorization)
        _require_scrape_manage(name, user)
        q = login_jobs.subscribe_frames(name)
        if q is None:
            raise HTTPException(status_code=404, detail=f"No remote sign-in running for {name!r}")
        viewport = _CONTEXT_OPTS["viewport"]

        def stream():
            yield f"data: {json.dumps({'type': 'meta', **viewport})}\n\n"
            while True:
                item = q.get()
                if item is login_jobs.FRAME_DONE:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                yield f"data: {json.dumps({'type': 'frame', 'data': item})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @api.post("/api/connectors/{name}/browser/session/input", tags=["Connectors"], summary="Send one input event (click/scroll/keystroke) to a running remote sign-in. 404 if none is running, 409 if this sign-in is a local window instead.")
    def browser_session_input(name: str, event: BrowserInputEvent,
                              authorization: str | None = Header(default=None)):
        from quickjoiner.connectors.browser import login_jobs

        user = _user(authorization)
        _require_scrape_manage(name, user)
        job = login_jobs.status(name)
        if job and job.summary()["active"] and job.mode != "remote":
            raise HTTPException(
                status_code=409,
                detail="This sign-in is a window on the QuickJoiner host — interact with that instead.",
            )
        if not login_jobs.push_input(name, event.model_dump(exclude_none=True)):
            raise HTTPException(status_code=404, detail=f"No remote sign-in running for {name!r}")
        return {"ok": True}

    @api.post("/api/connectors/{name}/browser/session/done", tags=["Connectors"], summary="Finish a running remote sign-in: capture the session and close it. 404 if none is running, 409 if this sign-in is a local window instead.")
    def browser_session_done(name: str, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors.browser import login_jobs

        user = _user(authorization)
        _require_scrape_manage(name, user)
        job = login_jobs.status(name)
        if job and job.summary()["active"] and job.mode != "remote":
            raise HTTPException(
                status_code=409,
                detail="This sign-in is a window on the QuickJoiner host — close that window instead.",
            )
        if not login_jobs.signal_done(name):
            raise HTTPException(status_code=404, detail=f"No remote sign-in running for {name!r}")
        return {"ok": True}

    # ---------------------------------------------------------------- Microsoft 365
    # Sign-in for the OneDrive/SharePoint connector. Two flows exist because
    # QuickJoiner runs in two places: this is the browser one (authorization code +
    # PKCE); the CLI uses device code (`qj onedrive login`), which needs no server
    # route at all. Pending flows are held in memory keyed by an unguessable state —
    # a restart mid-flow just means starting again, which is why nothing is persisted.
    _pending_oauth: dict[str, dict] = {}

    def _onedrive_connector(name: str, user: str | None):
        from quickjoiner.connectors.onedrive import OneDriveConnector
        from quickjoiner.connectors.registry import create_connector

        source = _find_source(name, user)
        connector = create_connector(source, ctx.workspace)
        if not isinstance(connector, OneDriveConnector):
            raise HTTPException(
                status_code=400,
                detail=f"{name!r} is a {source.type} connector, not a OneDrive/SharePoint one",
            )
        return source, connector

    @api.post("/api/connectors/{name}/oauth/start", tags=["Connectors"], summary="Begin Microsoft 365 sign-in for a OneDrive connector; returns the URL to send the browser to.")
    def oauth_start(name: str, request: Request, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors import msgraph

        user = _user(authorization)
        _require_user(user)
        _source, connector = _onedrive_connector(name, user)
        _require("connectors:write", user)
        if not can_manage(_source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can sign this connector in")
        if not connector.client_id:
            raise HTTPException(status_code=400, detail="Set the Application (client) ID first")
        verifier, challenge = msgraph.pkce_pair()
        state = secrets.token_urlsafe(24)
        redirect_uri = str(request.base_url).rstrip("/") + "/api/oauth/callback"
        _pending_oauth[state] = {
            "source_id": connector.source_id, "verifier": verifier,
            "redirect_uri": redirect_uri, "tenant": connector.tenant,
            "client_id": connector.client_id, "scopes": connector.scopes,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        return {
            "authorize_url": msgraph.authorize_url(
                connector.tenant, connector.client_id, redirect_uri,
                connector.scopes, state, challenge),
            "state": state,
            "redirect_uri": redirect_uri,
        }

    @api.get("/api/oauth/callback", response_class=HTMLResponse, tags=["Connectors"], summary="OAuth redirect target for Microsoft 365 sign-in — exchanges the code and stores the connector's token.")
    def oauth_callback(state: str = "", code: str = "", error: str = "",
                       error_description: str = ""):
        """Public by necessity: this is Microsoft redirecting a browser, which cannot
        carry a bearer token. The guard is `state` — minted here, unguessable, single
        use — so a caller who did not start the flow cannot complete one."""
        from quickjoiner.connectors import msgraph

        pending = _pending_oauth.pop(state, None)
        if error:
            return HTMLResponse(_oauth_page("Sign-in cancelled", error_description or error), 400)
        if pending is None:
            return HTMLResponse(
                _oauth_page("Sign-in expired",
                            "Start the sign-in again from Settings — this link is single use."), 400)
        try:
            bundle = msgraph.exchange_code(
                pending["tenant"], pending["client_id"], code, pending["redirect_uri"],
                pending["verifier"], pending["scopes"])
            client = msgraph.GraphClient(ctx.workspace, pending["source_id"], pending["scopes"])
            msgraph.save_token(ctx.workspace, pending["source_id"], bundle)
            me = client.whoami()
            bundle.account = me.get("userPrincipalName") or me.get("displayName") or ""
            bundle.tenant, bundle.client_id = pending["tenant"], pending["client_id"]
            msgraph.save_token(ctx.workspace, pending["source_id"], bundle)
        except msgraph.GraphError as exc:
            return HTMLResponse(_oauth_page("Sign-in failed", str(exc)), 400)
        return HTMLResponse(_oauth_page(
            f"Signed in as {bundle.account}",
            "You can close this tab and return to QuickJoiner."))

    @api.get("/api/connectors/{name}/oauth/status", tags=["Connectors"], summary="Whether a OneDrive connector is signed in to Microsoft 365, and as whom.")
    def oauth_status(name: str, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors import msgraph

        user = _user(authorization)
        _source, connector = _onedrive_connector(name, user)
        _require("connectors:read", user)
        bundle = msgraph.load_token(ctx.workspace, connector.source_id)
        if bundle is None:
            return {"signed_in": False, "account": "", "scopes": [], "learned": 0}
        from quickjoiner.connectors.onedrive import load_manifest

        # Deliberately never returns the tokens themselves — only who and what.
        return {
            "signed_in": True, "account": bundle.account, "scopes": bundle.scopes,
            "expires_at": bundle.expires_at,
            "learned": len(load_manifest(ctx.workspace, connector.source_id)),
        }

    @api.delete("/api/connectors/{name}/oauth", tags=["Connectors"], summary="Sign a OneDrive connector out of Microsoft 365 (deletes its stored refresh token).")
    def oauth_signout(name: str, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors import msgraph

        user = _user(authorization)
        _require_user(user)
        source, connector = _onedrive_connector(name, user)
        _require("connectors:write", user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can sign this connector out")
        return {"signed_out": msgraph.delete_token(ctx.workspace, connector.source_id)}

    @api.post("/api/connectors/{name}/onedrive/learn", tags=["Sync & ingestion"], summary="Learn specific OneDrive/SharePoint documents on demand — resolves each URL or path with the connector's own sign-in, extracts the text and ingests it into memory.")
    def onedrive_learn(name: str, req: OneDriveLearnRequest,
                       authorization: str | None = Header(default=None)):
        """The on-demand counterpart to a crawl: this connector ingests only what it is
        pointed at. `targets` accepts shared links (any OneDrive/SharePoint URL the
        signed-in user can open) and paths inside their own drive; a folder learns the
        supported files beneath it, bounded and reported."""
        from quickjoiner.connectors.msgraph import GraphError
        from quickjoiner.connectors.onedrive import is_communal_memory_warning

        user = _user(authorization)
        _require_user(user)
        source, connector = _onedrive_connector(name, user)
        _require("memory:write", user)
        targets = [t for t in (req.targets or []) if t and t.strip()]
        if not targets:
            raise HTTPException(status_code=400, detail="Give at least one URL or path to learn")
        try:
            documents, notes = connector.learn(targets)
        except GraphError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if not documents:
            return {"learned": 0, "documents": [], "notes": notes or ["Nothing could be learned"],
                    "warning": is_communal_memory_warning()}
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
        stats = ctx.pipeline.ingest(documents, connector.source_id)
        return {
            "learned": len(documents),
            "documents": [{"title": d.title, "uri": d.uri} for d in documents],
            "ingested": stats.summary(),
            "notes": notes,
            "warning": is_communal_memory_warning(),
        }

    @api.post("/api/connectors/{name}/test", tags=["Connectors"], summary="Test a connector's credentials / reachability without ingesting anything.")
    def test_connector(name: str, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors.registry import create_connector

        user = _user(authorization)
        source = _find_source(name, user)
        _require("connectors:write", user)
        result = create_connector(source, ctx.workspace).test()
        return {"ok": result.ok, "message": result.message}

    ui_dir = _ui_dir()
    if (ui_dir / "assets").is_dir():  # React build: hashed js/css bundles
        api.mount("/assets", StaticFiles(directory=ui_dir / "assets"), name="assets")

    @api.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return (ui_dir / "index.html").read_text(encoding="utf-8")

    @api.get("/health", tags=["Status"], summary="Liveness probe (no auth) — confirms the server is up.")
    def health():
        return {"status": "ok", "workspace": str(ctx.workspace), "org": ctx.config.org}

    @api.get("/api/status", tags=["Status"], summary="Workspace status: org name, active LLM model, and learned-memory counts (documents / chunks / sources).")
    def status():
        return {
            "org": ctx.config.org,
            "llm": {"provider": ctx.config.llm.provider, "model": ctx.config.llm.resolved_model()},
            "stats": ctx.catalog.stats(),
        }

    # -- workspace settings (stored in SQLite; tunable from the UI) ------------
    def _settings_view() -> dict:
        c = ctx.config
        return {
            "org": c.org,
            "llm": c.llm.model_dump(),
            "embedding": c.embedding.model_dump(),
            "retrieval": c.retrieval.model_dump(),
            "chat": c.chat.model_dump(),
            "graph": c.graph.model_dump(),
            "repos": c.repos.model_dump(),
            # Embedding changes only take effect after a restart + full re-sync
            # (existing vectors are in the old model's space) — the UI warns on this.
            "embedding_reindex_required": True,
        }

    @api.get("/api/settings", tags=["Settings"], summary="Get all tunable workspace settings (llm / embedding / retrieval / chat / graph / repos).")
    def get_settings():
        return _settings_view()

    @api.get("/api/settings/defaults", tags=["Settings"], summary="The shipped default settings, so a client can show which fields differ from stock.")
    def get_setting_defaults():
        """The shipped defaults, same shape as /api/settings. The Settings drawer marks
        each field that still sits at its default (and shows what the default was when it
        doesn't), so 'what have I actually changed here?' is answerable at a glance.
        Built from freshly-constructed config models, so it can never drift from the code."""
        fresh = Config(org=ctx.config.org)
        return {
            "llm": fresh.llm.model_dump(),
            "embedding": fresh.embedding.model_dump(),
            "retrieval": fresh.retrieval.model_dump(),
            "chat": fresh.chat.model_dump(),
            "graph": fresh.graph.model_dump(),
            "repos": fresh.repos.model_dump(),
        }

    @api.patch("/api/settings", tags=["Settings"], summary="Update workspace settings. Validated against the config models; persisted to the workspace.")
    def update_settings(req: SettingsUpdate, authorization: str | None = Header(default=None)):
        user = _user(authorization)
        _require_user(user)
        _require("settings:write", user)
        c = ctx.config
        try:
            if req.org is not None:
                c.org = req.org
            if req.llm:
                c.llm = LLMConfig.model_validate({**c.llm.model_dump(), **req.llm})
            if req.embedding:
                c.embedding = EmbeddingConfig.model_validate({**c.embedding.model_dump(), **req.embedding})
            if req.retrieval:
                c.retrieval = RetrievalConfig.model_validate({**c.retrieval.model_dump(), **req.retrieval})
            if req.chat:
                c.chat = ChatConfig.model_validate({**c.chat.model_dump(), **req.chat})
            if req.graph:
                c.graph = GraphConfig.model_validate({**c.graph.model_dump(), **req.graph})
            if req.repos:
                c.repos = ReposConfig.model_validate({**c.repos.model_dump(), **req.repos})
        except Exception as exc:  # pydantic validation error -> bad input
            raise HTTPException(status_code=400, detail=str(exc))
        ctx.catalog.save_config(c)  # persist; live agents read ctx.config on next build
        # Re-derive the store + ingest pipeline, which are BUILT from config rather than
        # reading it per call — without this a graph/retrieval toggle only took effect
        # after a server restart, and a re-sync in between silently used the old settings.
        ctx.apply_config()
        return _settings_view()

    @api.post("/api/llm/test", tags=["Settings"], summary="Probe the configured LLM provider with a one-token round-trip. Accepts UNSAVED llm overrides so a client can verify a proxy/model before saving.")
    def test_llm(req: LLMTestRequest, authorization: str | None = Header(default=None)):
        """Probe the configured LLM provider with a one-token round-trip. Accepts
        optional `llm` overrides so the settings form can test UNSAVED values (proxy
        URL, model, api-key env var) before persisting. Never saves."""
        user = _user(authorization)
        _require_user(user)
        _require("settings:write", user)
        from quickjoiner.llm import create_provider

        try:
            llm_cfg = LLMConfig.model_validate({**ctx.config.llm.model_dump(), **(req.llm or {})})
        except Exception as exc:  # invalid overrides
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            provider = create_provider(llm_cfg)
            result = provider.chat(
                [{"role": "user", "content": "Reply with the single word: OK"}],
                system="You are a connection health check. Answer in one word.",
            )
        except Exception as exc:
            return {"ok": False, "model": llm_cfg.resolved_model(), "message": str(exc)[:300]}
        reply = (result.text or "").strip()
        return {
            "ok": True,
            "model": llm_cfg.resolved_model(),
            "message": f"Reached {llm_cfg.provider} · replied “{reply[:60] or '(empty)'}”",
        }

    @api.get("/api/sources", tags=["Connectors"], summary="List every source with its document count — configured connectors plus ingestion buckets (taught notes, webhook pushes).")
    def sources(authorization: str | None = Header(default=None)):
        user = _user(authorization)
        configured = {s.name: s for s in ctx.config.sources}
        rows = []
        for s in ctx.catalog.list_sources():
            cfg = configured.get(s["name"])
            # Catalog-only rows (taught notes, webhook pushes) are commons;
            # configured rows follow the source's visibility.
            if cfg is not None and not visible(cfg, user, auth.enabled):
                continue
            rows.append(
                {
                    "id": s["id"],
                    "name": s["name"],
                    "type": s["type"],
                    "documents": s["doc_count"],
                    "configured": cfg is not None,
                }
            )
        return rows

    def _resolve_scope(req_scope):
        """`ScopeRequest` -> `(SearchScope | None, prompt_note)`.

        The note matters as much as the filter: a model that doesn't know it is looking at a
        slice will report "not learned" as if it had searched everything. Telling it the
        scope is what keeps a scoped refusal honest.
        """
        from quickjoiner.memory.store import SearchScope

        if req_scope is None:
            return None, ""
        source_ids, doc_ids = ctx.catalog.resolve_scope(
            req_scope.source_ids, req_scope.tags, req_scope.doc_ids)
        scope = SearchScope(source_ids=source_ids, doc_ids=doc_ids)
        if scope.is_empty():
            return None, ""
        described = ", ".join(
            [*(s.split(":", 1)[-1] for s in source_ids),
             *(f"tag '{t}'" for t in req_scope.tags),
             *([f"{len(doc_ids)} selected document(s)"] if doc_ids else [])]
        )
        note = (
            f"SCOPED QUESTION — the user restricted this question to: {described}. "
            "Memory search is already filtered to it, and live tools for other systems are "
            "not available this turn. If the answer isn't in this slice, say it isn't in the "
            "sources they scoped to and offer to search everything — do NOT say the "
            "organisation never learned it, because you only looked at part of memory."
        )
        return scope, note

    def _source_visible(source_id: str, user: str | None) -> bool:
        """A source_id (`type:name`) is readable if it isn't a configured connector (an
        ingestion bucket is commons) or its config says this user may see it."""
        name = source_id.split(":", 1)[-1]
        cfg = next((s for s in ctx.config.sources if f"{s.type}:{s.name}" == source_id
                    or s.name == name), None)
        return cfg is None or visible(cfg, user, auth.enabled)

    def _labelled(source_id: str, rows: list[dict]) -> list[dict]:
        """Attach each document's applicable labels. Labels are prefix RULES, so they are
        resolved against every document's uri here rather than stored per document — which
        is what lets a folder label cover files ingested after it was created."""
        from quickjoiner.memory.catalog import label_applies

        labels = ctx.catalog.labels_for_source(source_id)
        out = []
        for row in rows:
            uri = row.get("uri") or ""
            applies = [
                {"kind": lb["kind"], "value": lb["value"], "uri_prefix": lb.get("uri_prefix") or ""}
                for lb in labels if label_applies(uri, lb.get("uri_prefix") or "")
            ]
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                metadata = {}  # tolerate a malformed blob rather than 500ing the whole list
            out.append({
                "doc_id": row["doc_id"], "uri": uri, "title": row.get("title") or "",
                "kind": row.get("kind") or "doc", "chunks": row.get("chunk_count") or 0,
                "updated_at": row.get("updated_at"), "labels": applies,
                # Connector-supplied display metadata (e.g. ADO work-item type/state/team/
                # sprint/parent id) — {} for every connector that doesn't set it.
                "metadata": metadata,
            })
        return out

    @api.get("/api/sources/{source_id:path}/documents", tags=["Ask & search"], summary="List the documents a source has ingested, each with the tags/aka labels that apply to it and any connector-supplied display metadata.")
    def source_documents(source_id: str, authorization: str | None = Header(default=None)):
        """What this connector actually learned — the answer to "what's in there?".

        Labels are resolved per document at read time (see `_labelled`), so a folder tag
        shows up on files that were ingested long after it was set. `metadata` is a small
        connector-supplied JSON blob for UI display only — e.g. the Azure DevOps connector
        stamps work-item type/state/team/sprint/parent id there, which is what the document
        browser uses to render TFS work items as an Epic/Feature/Story/Task tree. `{}` for
        every connector that doesn't set it."""
        user = _user(authorization)
        _require("search:read", user)
        if not _source_visible(source_id, user):
            raise HTTPException(status_code=404, detail=f"No source {source_id!r}")
        rows = ctx.catalog.documents_for_source(source_id)
        return {"source_id": source_id, "documents": _labelled(source_id, rows),
                "labels": ctx.catalog.labels_for_source(source_id)}

    @api.get("/api/sources/{source_id:path}/documents/archive", tags=["Ask & search"], summary="List the member files inside an ingested archive (.zip) document, recovered from its extracted text.")
    def document_archive(source_id: str, doc_id: str, authorization: str | None = Header(default=None)):
        """A .zip ingests as ONE document (its members' text concatenated — see
        `ingest/extract._extract_zip`), so the document browser otherwise shows it as one
        opaque row. There's no separate column for the member list (chunks are the only
        place a document's text lives), so this re-derives it from the same `--- path ---`
        headers extraction wrote, via `parse_archive_manifest`. Empty members/notes for a
        non-archive document — that's a normal answer, not an error."""
        from quickjoiner.ingest.extract import parse_archive_manifest

        user = _user(authorization)
        _require("search:read", user)
        if not _source_visible(source_id, user):
            raise HTTPException(status_code=404, detail=f"No source {source_id!r}")
        rows = ctx.catalog.documents_for_source(source_id)
        if not any(r["doc_id"] == doc_id for r in rows):
            raise HTTPException(status_code=404, detail="No such document in this source")
        text = "\n\n".join(ctx.store.get_document_chunks(doc_id))
        return parse_archive_manifest(text)

    @api.get("/api/labels", tags=["Ask & search"], summary="Every tag/aka label in the workspace — what the question-scope picker offers.")
    def list_labels(authorization: str | None = Header(default=None)):
        user = _user(authorization)
        _require("search:read", user)
        rows = [lb for lb in ctx.catalog.all_labels() if _source_visible(lb["source_id"], user)]
        return {"labels": rows}

    @api.post("/api/labels", tags=["Ask & search"], summary="Tag a connector, a folder, or a single document (a folder tag covers files ingested later).")
    def add_label(req: LabelRequest, authorization: str | None = Header(default=None)):
        """`uri_prefix` chooses the granularity: '' = the whole connector, a folder path =
        that folder and anything ingested into it later, a full document uri = just that one.
        `kind` is 'tag' (scoping label) or 'aka' (an alternate name, which also feeds alias
        query expansion so a loose question can find the document)."""
        user = _user(authorization)
        _require_user(user)
        _require("memory:write", user)
        if req.kind not in ("tag", "aka"):
            raise HTTPException(status_code=400, detail="kind must be 'tag' or 'aka'")
        if not req.value.strip():
            raise HTTPException(status_code=400, detail="value cannot be empty")
        if not _source_visible(req.source_id, user):
            raise HTTPException(status_code=404, detail=f"No source {req.source_id!r}")
        ctx.catalog.set_label(req.source_id, req.uri_prefix, req.kind, req.value)
        if req.kind == "aka" and not req.uri_prefix:
            # A whole-connector 'aka' is exactly what the connector `aka` option already
            # declares, so route it through the same machinery: an alias on the source
            # entity, which feeds resolve_entity + alias query expansion + autocomplete.
            # Folder/document-level akas are NOT registered this way — they name a document,
            # not the source entity, and claiming otherwise would misdirect the graph.
            try:
                from quickjoiner.ingest.pipeline import source_entity

                eid, ename, kind = source_entity(req.source_id)
                ctx.catalog.upsert_entity(eid, ename, kind, req.source_id)
                ctx.catalog.add_entity_alias(req.value.strip(), eid)
            except Exception:  # noqa: BLE001 — the label is saved either way
                pass
        return {"ok": True, "labels": ctx.catalog.labels_for_source(req.source_id)}

    @api.delete("/api/labels", tags=["Ask & search"], summary="Remove a tag/aka label from a connector, folder, or document.")
    def remove_label(req: LabelRequest, authorization: str | None = Header(default=None)):
        user = _user(authorization)
        _require_user(user)
        _require("memory:write", user)
        if not _source_visible(req.source_id, user):
            raise HTTPException(status_code=404, detail=f"No source {req.source_id!r}")
        ctx.catalog.remove_label(req.source_id, req.uri_prefix, req.kind, req.value)
        return {"ok": True, "labels": ctx.catalog.labels_for_source(req.source_id)}

    @api.post("/api/sync/{source_name}", tags=["Sync & ingestion"], summary="Start a background sync job for a source. clean=true purges the source first for a from-scratch re-pull. Returns immediately with the job.")
    def sync_source(source_name: str, clean: bool = False,
                    authorization: str | None = Header(default=None)):
        """Start a background sync job (returns immediately). `clean=true` purges the
        source's documents/vectors/graph first for a from-scratch, non-corrupted resync.
        Multiple different sources can sync at once; watch progress on the logs stream."""
        _guard_not_control(source_name, "synced (it has nothing to ingest)")
        user = _user(authorization)
        _find_source(source_name, user)  # visibility + existence gate
        _require("sync:run", user)
        try:
            job = syncs.start(source_name, clean=clean)
        except RuntimeError as exc:  # already running
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job": job.summary()}

    @api.post("/api/sync/{source_name}/stop", tags=["Sync & ingestion"], summary="Stop a running sync. cleanup=true also purges whatever the interrupted run ingested. Takes effect within seconds even mid-pull.")
    def stop_sync(source_name: str, cleanup: bool = False,
                  authorization: str | None = Header(default=None)):
        """Stop a running sync. `cleanup=true` also purges whatever the interrupted run
        ingested, leaving the source (and the knowledge graph) clean rather than partial."""
        user = _user(authorization)
        _find_source(source_name, user)
        _require("sync:run", user)
        try:
            job = syncs.stop(source_name, cleanup=cleanup)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"job": job.summary()}

    @api.post("/api/sync/{source_name}/pause", tags=["Sync & ingestion"], summary="Pause a running sync in place (holds both the pull and the graph-extraction tail). The same in-memory run continues on resume — nothing re-pulls.")
    def pause_sync(source_name: str, authorization: str | None = Header(default=None)):
        """Hold a running sync in place (after the current document / between graph
        batches). Committed work stays; resume continues the same in-memory run."""
        user = _user(authorization)
        _find_source(source_name, user)
        _require("sync:run", user)
        try:
            job = syncs.pause(source_name)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job": job.summary()}

    @api.post("/api/sync/{source_name}/resume", tags=["Sync & ingestion"], summary="Resume a paused sync.")
    def resume_sync(source_name: str, authorization: str | None = Header(default=None)):
        """Resume a paused sync."""
        user = _user(authorization)
        _find_source(source_name, user)
        _require("sync:run", user)
        try:
            job = syncs.resume(source_name)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job": job.summary()}

    @api.get("/api/syncs", tags=["Sync & ingestion"], summary="Live state of every sync job in this server process (running + finished this session), including current stage and estimated %.")
    def list_syncs(authorization: str | None = Header(default=None)):
        """State of every sync job (running + finished this session)."""
        return {"syncs": syncs.status()}

    @api.post("/api/memory/reset", tags=["Sync & ingestion"], summary="DANGER: wipe ALL ingested knowledge — documents, vectors, the whole knowledge graph, and every sync watermark — leaving the workspace as if nothing had synced. KEEPS connectors, users, chat, and settings. Runs as a background job (returns {job}); watch it via the logs SSE / notifications. Refuses (409) while any sync is running.")
    def reset_memory(authorization: str | None = Header(default=None)):
        """Wipe ALL ingested knowledge — documents, vectors, FTS, the whole knowledge
        graph, and every sync watermark — leaving the workspace as if nothing had synced.
        KEEPS connector configs, users, chat sessions/projects, and settings; the next
        sync of each connector is a full pull. Runs as a **background job** (returns
        `{job}`, source `"all memory"`) so it streams logs and lands in the activity feed —
        watch it via `GET /api/sync/all%20memory/logs` or `/api/notifications`. Refuses
        (409) while any sync/cleanup job is active, since it clears every source at once."""
        user = _user(authorization)
        _require_user(user)
        _require("memory:reset", user)
        try:
            job = syncs.start_reset()  # runs as a background job → logs, notifications, history
        except RuntimeError as exc:  # a sync/cleanup is in flight
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job": job.summary()}

    @api.get("/api/notifications", tags=["Sync & ingestion"], summary="Sync activity feed over the last N hours (default 24) — running jobs with live state plus finished ones from persisted history. Survives a server restart.")
    def notifications(hours: int = 24, authorization: str | None = Header(default=None)):
        """Sync activity over the last `hours` (default 24), newest first — running jobs
        with live state plus finished ones from the persisted history. Backs the
        notification menu; read-state is tracked client-side."""
        _user(authorization)
        items = syncs.recent(hours=max(1, min(hours, 24 * 7)))
        active = sum(1 for i in items if i["state"] in ("running", "stopping", "retrying"))
        return {"notifications": items, "active": active}

    @api.get("/api/sync/{source_name}/logs", tags=["Sync & ingestion"], summary="Server-Sent Events stream of a sync's live log lines (replays the backlog first), ending with a terminal `done` event.")
    def sync_logs(source_name: str, authorization: str | None = Header(default=None)):
        """SSE stream of a job's live log lines (replays the backlog first), then a
        terminal `done` event carrying the job's final state. Serves sync/cleanup jobs and
        the manager-only jobs that aren't a configured source — the memory reset, and a
        cleanup for a connector that was just deleted."""
        user = _user(authorization)
        # Honor visibility for a real configured source; manager-only jobs (reset / a
        # just-deleted connector's cleanup) aren't in the config, so gate them on auth only.
        src = next((s for s in ctx.config.sources if s.name == source_name), None)
        if src is not None and not visible(src, user, auth.enabled):
            raise HTTPException(status_code=404, detail=f"No configured source {source_name!r}")
        if src is None:
            _require_user(user)
        job, q = syncs.subscribe(source_name)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No sync for {source_name!r}")

        def stream():
            while True:
                item = q.get()
                if item is _DONE:
                    final = syncs.job_for(source_name)
                    yield f"data: {json.dumps({'type': 'done', 'job': final.summary() if final else None})}\n\n"
                    return
                yield f"data: {json.dumps({'type': 'log', 'line': item})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @api.post("/api/learn", tags=["Ask & search"], summary="Teach the system a fact or ingest a free-text note directly into memory (no LLM round-trip).")
    def learn(req: LearnRequest, authorization: str | None = Header(default=None)):
        """Teach qj a free-text fact from the UI — same store as the agent's
        `remember` tool and `qj learn "<text>"`, no LLM round-trip needed."""
        from quickjoiner.agent.tools import teach_fact

        user = _user(authorization)
        _require_user(user)
        _require("memory:write", user)
        fact = req.fact.strip()
        if not fact:
            raise HTTPException(status_code=400, detail="Nothing to learn: empty fact")
        return {"result": teach_fact(ctx.catalog, ctx.pipeline, fact, req.topic)}

    def _ingest_upload_path(path: Path, progress_cb=None) -> dict:
        """Ingest one file that already lives in the uploads folder into the rolling uploads
        source. Uses the SAME reader (and thus the same doc uri/id) a folder sync would, so a
        later `sync uploads` is idempotent. Returns a per-file result row.

        `progress_cb(done, total)` (optional) is threaded straight to `pipeline.ingest` — see
        its doc comment. Only `learn_attachment` below passes one; every other caller is
        unaffected."""
        from quickjoiner.connectors.files import read_file_document
        from quickjoiner.connectors.uploads import UPLOADS_NAME, UPLOADS_SOURCE_ID, UPLOADS_TYPE, uploads_dir

        ctx.catalog.upsert_source(UPLOADS_SOURCE_ID, UPLOADS_NAME, UPLOADS_TYPE)
        doc = read_file_document(path, uploads_dir(ctx.workspace))
        if doc is None:
            return {"file": path.name, "ingested": False,
                    "reason": "unsupported type, empty, or no extractable text (image-only?)"}
        stats = ctx.pipeline.ingest([doc], UPLOADS_SOURCE_ID, progress_cb=progress_cb)
        return {"file": path.name, "title": doc.title, "ingested": True, "result": stats.summary()}

    @api.post("/api/uploads", tags=["Sync & ingestion"], summary="Upload one or more documents (Word, PowerPoint, Excel, PDF, Markdown, text, JSON, HTML, code) straight into memory via the rolling Uploads connector. Text is extracted at ingest.")
    async def upload_documents(
        files: list[UploadFile] = File(...), authorization: str | None = Header(default=None)
    ):
        """Off-hand document uploads (chat drag-drop, /qj, API): each file is saved into the
        managed `<workspace>/uploads/` folder and ingested into the single rolling uploads
        source, so it becomes cited memory immediately and persists for future re-syncs."""
        from quickjoiner.connectors.uploads import save_upload

        user = _user(authorization)
        _require_user(user)
        _require("memory:write", user)
        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")
        results = []
        for f in files:
            data = await f.read()
            if not data:
                results.append({"file": f.filename or "?", "ingested": False, "reason": "empty file"})
                continue
            # save_upload + _ingest_upload_path are synchronous and, for a large office doc or
            # archive, slow (extraction, chunking, embedding) — off the event loop via a thread
            # pool so one big upload doesn't stall every other request/SSE stream on the server
            # for the duration (an `async def` handler blocks the loop for anything it awaits
            # directly; only run_in_threadpool actually frees it up).
            path = await run_in_threadpool(save_upload, ctx.workspace, f.filename or "upload", data)
            results.append(await run_in_threadpool(_ingest_upload_path, path))
        return {"uploaded": results, "ingested": sum(1 for r in results if r.get("ingested"))}

    @api.post("/api/uploads/local", tags=["Sync & ingestion"], summary="Ingest a document from a server-side file path into the rolling Uploads connector (the /qj-friendly path — the file is copied into the uploads folder).")
    def upload_local(req: UploadLocalRequest, authorization: str | None = Header(default=None)):
        """Ingest a file the server can already read (a local path) into the rolling uploads
        source — the JSON path the `/qj` control tool uses, since it can't carry binary. The file
        is copied into the managed uploads folder so it becomes part of the rolling source."""
        from quickjoiner.connectors.uploads import save_upload

        user = _user(authorization)
        _require_user(user)
        _require("memory:write", user)
        src = Path(req.path.strip().strip('"'))
        if not src.is_file():
            raise HTTPException(status_code=400, detail=f"Not a readable file: {src}")
        try:
            data = src.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Could not read {src}: {exc}")
        path = save_upload(ctx.workspace, src.name, data)
        return _ingest_upload_path(path)

    @api.post("/api/chat/attachments/{att_id}/learn", tags=["Sync & ingestion"], summary="Promote a chat attachment into permanent memory — ingests the already-uploaded file into the rolling Uploads connector.")
    def learn_attachment(
        att_id: str, progress_token: str | None = None, authorization: str | None = Header(default=None)
    ):
        """Turn a per-question attachment into learned memory, deliberately as a separate,
        explicit action.

        Attaching a file in chat is *context for one question* and nothing more — that
        separation is on purpose. But "here's a document, learn it" is the obvious thing to
        want once you've attached one, and before this the only route was re-uploading the
        same bytes through a different endpoint. This promotes what is already on disk: no
        re-upload, and the attachment stays a downloadable attachment as well.

        `progress_token` (optional, minted by the frontend) makes this request's real chunk-
        embedding progress readable via `GET /api/ingest-progress/{token}` while this call is
        still in flight — see `ingest_progress.py`. Omit it and this behaves exactly as before
        (used by the CLI/agent's `qj_api` tool, which just wants the final result).
        """
        from quickjoiner import chat_attachments, ingest_progress
        from quickjoiner.connectors.uploads import save_upload

        user = _user(authorization)
        _require_user(user)
        _require("memory:write", user)
        row = ctx.catalog.get_context_attachment(att_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such attachment")
        src = chat_attachments.attachment_original_path(ctx, att_id)
        if src is None:
            # The retention sweep removes the bytes but keeps the row, so history can still
            # show the filename — 410 says "it existed and is gone", which 404 would not.
            raise HTTPException(
                status_code=410,
                detail=f"{row['filename']} has expired and its file was deleted — re-attach it to learn it.")
        path = save_upload(ctx.workspace, row["filename"], src.read_bytes())
        progress_cb = None
        if progress_token:
            ingest_progress.start(progress_token)
            progress_cb = lambda done, total: ingest_progress.update(progress_token, done, total)  # noqa: E731
        return _ingest_upload_path(path, progress_cb=progress_cb)

    @api.get("/api/ingest-progress/{token}", tags=["Sync & ingestion"], summary="Poll real chunk-embedding progress for an in-flight ad-hoc ingest started with that progress_token.")
    def ingest_progress_status(token: str, authorization: str | None = Header(default=None)):
        """204 (no body) if the token is unknown — never started, already finished and aged
        out, or nobody ever will start it. The frontend treats that as "nothing to show yet",
        not an error; it's racing this against the POST that owns the token."""
        from quickjoiner import ingest_progress

        user = _user(authorization)
        _require_user(user)
        _require("chat:use", user)
        row = ingest_progress.get(token)
        if row is None:
            return Response(status_code=204)
        return row

    @api.get("/api/gaps", tags=["Knowledge gaps"], summary="Clusters of questions the system could not answer (the knowledge-debt backlog), with suggested connectors/actions to close them.")
    def list_gaps(authorization: str | None = Header(default=None)):
        """The knowledge-debt backlog: open refusals clustered by topic, each with
        suggested connectors/entities to remediate. Query text is omitted in
        hash-only privacy mode (gaps.store_queries=false)."""
        _require_user(_user(authorization))
        if not ctx.config.gaps.enabled:
            return {"open_count": 0, "clusters": []}
        from quickjoiner.gaps import cluster_gaps

        rows = ctx.catalog.list_gaps("open")
        clusters = cluster_gaps(
            rows, ctx.store.embedder, ctx.config.gaps.cluster_threshold, ctx.catalog
        )
        return {"open_count": len(rows), "clusters": clusters}

    @api.post("/api/gaps/resolve", tags=["Knowledge gaps"], summary="Mark one or more gap clusters resolved or dismissed.")
    def resolve_gaps(req: GapsResolveRequest, authorization: str | None = Header(default=None)):
        """Mark gaps resolved (after connecting a source, teaching, or dismissing)."""
        user = _user(authorization)
        _require_user(user)
        _require("gaps:write", user)
        ctx.catalog.resolve_gaps(req.gap_ids, req.resolution or "dismissed")
        return {"resolved": len(req.gap_ids)}

    @api.get("/api/suggest", tags=["Ask & search"], summary="Deterministic question-autocomplete suggestions computed live from the knowledge graph — powers the composer typeahead.")
    def suggest(q: str = "", limit: int = 6):
        """Question autocomplete as the user types — keyless/deterministic, drawn
        from the knowledge graph (entity-templated questions), past questions, and
        source-aware starters. Fast enough for per-keystroke use (no LLM)."""
        return {"suggestions": suggester.suggest(q, limit=max(1, min(limit, 10)))}

    @api.post("/api/scrape", tags=["Ask & search"], summary="Crawl a URL into a single cited report WITHOUT ingesting it (learning is explicit). Streams SSE: status / delta / answer / done.")
    def scrape(req: ScrapeRequest, authorization: str | None = Header(default=None)):
        """SSE: crawl a URL (depth-limited), synthesize a markdown+mermaid report.
        Events: status (progress lines), delta (streamed synthesis), answer
        (final markdown + saved path), error, done. Pages are NOT auto-ingested —
        the UI offers an explicit "learn this report" step instead."""
        from quickjoiner.agent.scrape_report import build_report, save_report
        from quickjoiner.connectors.registry import create_connector

        user = _user(authorization)
        _require_user(user)
        _require("scrape:run", user)
        url = req.url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Provide an http(s):// URL to scrape")

        events: queue.Queue = queue.Queue()

        def worker():
            try:
                source = SourceConfig(
                    name="adhoc-scrape", type="web_scrape",
                    options={"start_urls": url, "max_depth": req.depth,
                             "max_pages": req.max_pages},
                )
                connector = create_connector(source, ctx.workspace)
                events.put({"type": "status",
                            "data": f"Crawling {url} — depth {req.depth}, up to {req.max_pages} pages…"})
                pages = []
                for doc in connector.sync({}):
                    pages.append(doc)
                    events.put({"type": "status", "data": f"[{len(pages)}] {doc.title[:90]}"})
                if not pages:
                    events.put({"type": "error",
                                "data": "No readable pages found — the site may block automated "
                                        "access or have no textual content."})
                    return
                provider = None
                try:
                    provider = ctx.build_provider(req.provider, req.model)
                except Exception:
                    events.put({"type": "status",
                                "data": "No LLM provider available — building a deterministic digest."})
                events.put({"type": "status", "data": f"Synthesizing report from {len(pages)} pages…"})

                def stream_delta(kind: str, delta: str) -> None:
                    if kind == "text":
                        events.put({"type": "delta", "data": delta})

                markdown = build_report(url, pages, provider=provider, on_stream=stream_delta)
                path = save_report(ctx.workspace, url, markdown)
                events.put({"type": "answer", "data": markdown,
                            "path": str(path), "pages": len(pages)})
            except Exception as exc:
                events.put({"type": "error", "data": str(exc)})
            finally:
                events.put(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()

        def stream():
            while True:
                item = events.get()
                if item is _SENTINEL:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                yield f"data: {json.dumps(item)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @api.get("/api/graph/path", tags=["Knowledge graph"], summary="Find evidence-cited path(s) between two entities. Surfaces materially different chains with per-chain confidence when they exist.")
    def graph_path(a: str, b: str, max_hops: int = 3):
        """Shortest recorded relationship chain between two entities (alias-resolved),
        each hop with its evidence document. 404 on unknown entity; path=null when
        no chain is recorded within max_hops."""
        ent_a = ctx.catalog.resolve_entity(a)
        ent_b = ctx.catalog.resolve_entity(b)
        for raw, ent in ((a, ent_a), (b, ent_b)):
            if ent is None:
                raise HTTPException(status_code=404, detail=f"No entity {raw!r} in the graph")
        path = ctx.catalog.graph_path(ent_a["id"], ent_b["id"], max_hops)
        def _node(e):
            return {"id": e["id"], "name": e["name"], "type": e["type"]}
        return {"a": _node(ent_a), "b": _node(ent_b),
                "path": None if path is None else [
                    {"src": r["src"], "rel": r["rel"], "dst": r["dst"], "detail": r["detail"],
                     "evidence": {"doc_id": r["evidence_doc_id"], "title": r["evidence_title"],
                                  "uri": r["evidence_uri"], "kind": r["evidence_kind"]}}
                    for r in path
                ]}

    @api.get("/api/graph", tags=["Knowledge graph"], summary="Graph snapshot — the whole graph (capped, fairly sampled across sources) or, with ?entity=, one entity's neighborhood with per-edge evidence.")
    def graph(entity: str | None = None, limit: int = 400):
        """Knowledge-graph snapshot: one entity's neighborhood (name/alias/id
        resolved) or the whole graph capped at `limit` edges. Every edge carries
        its evidence document for citations."""
        if entity:
            ent = ctx.catalog.resolve_entity(entity)
            if ent is None:
                raise HTTPException(status_code=404, detail=f"No entity {entity!r} in the graph")
            return {"entity": {"id": ent["id"], "name": ent["name"], "type": ent["type"]},
                    **ctx.catalog.graph_snapshot(ent["id"], limit)}
        return ctx.catalog.graph_snapshot(None, limit)

    @api.get("/api/graph/pending", tags=["Knowledge graph"], summary="How many ingested documents still have unmined relationships (deferred LLM triple extraction that never resolved), broken down by source.")
    def graph_pending(authorization: str | None = Header(default=None)):
        """Documents whose deferred graph work never landed. A connector that ingests a
        moving window (recent sprints) never re-yields them, so they stay queued forever
        with chunks and citations but no edges — `POST /api/graph/drain` finishes them."""
        _user(authorization)
        return {
            "total": ctx.catalog.count_graph_pending(),
            "by_source": [
                {"source_id": r["source_id"], "count": int(r["n"])}
                for r in ctx.catalog.graph_pending_by_source()
            ],
            "extraction_enabled": bool(getattr(ctx.pipeline, "extracts_triples", False)),
        }

    @api.post("/api/graph/drain", tags=["Knowledge graph"], summary="Mine the relationships of documents whose deferred graph work never landed, without a connector round-trip. Runs as a background job (returns {job}, source \"graph relationships\"). 409 while any other job is running or when LLM triple extraction is off.")
    def graph_drain(source_id: str | None = None,
                    authorization: str | None = Header(default=None)):
        """Re-read the text already indexed for each queued document and resolve its
        relationships in place. Optionally scoped to one `source_id`. Runs as a background
        job so it streams logs and lands in the activity feed — it is one LLM call per
        document. Refuses (409) while another job is in flight (it writes edges across
        sources) or when `graph.extract_triples` is off (nothing could resolve)."""
        user = _user(authorization)
        _require_user(user)
        _require("sync:run", user)
        try:
            job = syncs.start_drain(source_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job": job.summary()}

    @api.get("/api/graph/search", tags=["Knowledge graph"], summary="Entity autocomplete over the graph (name/alias substring, ranked by connectivity).")
    def graph_search(q: str, limit: int = 10):
        """Entity autocomplete for the graph view's search box — substring match
        over names/aliases, not the exact resolve /api/graph does."""
        return ctx.catalog.search_entities(q, limit)

    @api.get("/api/graph/bridges", tags=["Knowledge graph"], summary="Entities that bridge multiple sources — the cross-source connective tissue of the evidence graph.")
    def graph_bridges(limit: int = 20):
        """Entities touched by more than one source's edges — cross-source
        correlation, and a much better "where do I start?" list than a slice of
        the raw graph."""
        return ctx.catalog.bridge_entities(limit)

    @api.get("/api/documents/{doc_id}/file", tags=["Briefs & repo docs"], summary="Return the local file content backing a citation, when the source keeps a real checkout (git clone / local files). 404 when there's no local file.")
    def document_file(doc_id: str):
        """The current local file content backing a citation, for connector
        types that keep a real checkout (git clones, a local files/ source) —
        so evidence chips can show the file in-app instead of only linking to
        a remote host, when the content is already sitting in the workspace.
        404 (not just an empty result) when there's no local file to show, so
        the frontend can fall back to the remote link cleanly."""
        from quickjoiner.connectors.local_view import local_file_path

        doc = ctx.catalog.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Unknown document")
        path = local_file_path(ctx.workspace, doc["source_id"], doc["uri"])
        if path is None:
            raise HTTPException(status_code=404, detail="No local file for this document")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=404, detail=f"Could not read file: {exc}") from exc
        return {"path": str(path), "title": doc["title"], "text": text}

    @api.get("/api/search", tags=["Ask & search"], summary="Hybrid semantic + keyword search over learned memory. Returns scored hits with source URIs — the retrieval layer beneath the agent, without an LLM call.")
    def search(q: str, top_k: int = 8):
        search_q = q
        if ctx.config.retrieval.alias_expansion:
            try:
                from quickjoiner.memory.expansion import expand_query

                search_q = expand_query(ctx.catalog, q)
            except Exception:  # expansion is best-effort; never break a search on it
                search_q = q
        hits = ctx.store.search(search_q, top_k=top_k, min_score=ctx.config.retrieval.min_score)
        return [
            {"score": round(h.score, 3), "title": h.title, "uri": h.uri, "kind": h.kind,
             "text": h.text[:500]}
            for h in hits
        ]

    @api.get("/api/briefs", tags=["Briefs & repo docs"], summary="List the onboarding briefs generated for this workspace.")
    def list_briefs():
        briefs_dir = ctx.workspace / "briefs"
        files = sorted(briefs_dir.glob("*.md")) if briefs_dir.exists() else []
        return [{"name": f.stem, "path": str(f)} for f in files]

    @api.post("/api/briefs/{brief_type}", tags=["Briefs & repo docs"], summary="Generate a cited onboarding brief (architecture / week1 / roadmap / quick-wins) from memory.")
    def make_brief(brief_type: str, provider: str | None = None, model: str | None = None,
                   authorization: str | None = Header(default=None)):
        from quickjoiner.agent.briefs import generate_brief

        _require("briefs:write", _user(authorization))
        try:
            markdown, path = generate_brief(
                ctx, brief_type, provider_override=provider, model_override=model
            )
        except ValueError as exc:  # unknown brief type
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # provider/setup failures
            raise HTTPException(status_code=502, detail=str(exc))
        return {"brief": markdown, "path": str(path) if path else None, "generated": path is not None}

    @api.post("/api/repos/{source_name}/agents-md", tags=["Briefs & repo docs"], summary="Generate a principal-engineer architecture brief (AGENTS.md) for a git/files repo from its real code structure + docs.")
    def make_agents_md(source_name: str, provider: str | None = None, model: str | None = None,
                       authorization: str | None = Header(default=None)):
        from quickjoiner.agent.repo_docs import generate_agents_md

        _require("briefs:write", _user(authorization))
        try:
            markdown, path = generate_agents_md(
                ctx, source_name, provider_override=provider, model_override=model
            )
        except ValueError as exc:  # unknown/unsynced source
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # provider/setup failures
            raise HTTPException(status_code=502, detail=str(exc))
        return {"brief": markdown, "path": str(path)}

    @api.get("/api/projects", tags=["Sessions & projects"], summary="List conversation projects (framing + memory scope for chats).")
    def list_projects():
        return ctx.catalog.list_projects()

    @api.post("/api/projects", tags=["Sessions & projects"], summary="Create a conversation project.")
    def create_project(req: ProjectRequest, authorization: str | None = Header(default=None)):
        _require("sessions:write", _user(authorization))
        return manager.create_project(req.name, req.description)

    @api.get("/api/sessions", tags=["Sessions & projects"], summary="List persisted chat sessions (optionally filtered by project).")
    def list_sessions(project: str | None = None):
        project_row = manager.resolve_project(project)
        if project and not project_row:
            raise HTTPException(status_code=404, detail=f"No project {project!r}")
        return ctx.catalog.list_sessions(project_row["id"] if project_row else None)

    @api.get("/api/sessions/{session_id}", tags=["Sessions & projects"], summary="Get a session's stored messages + rolling summary.")
    def get_session(session_id: str):
        session = ctx.catalog.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
        from quickjoiner import chat_attachments

        out_messages = []
        for m in json.loads(session.pop("messages_json")):
            entry = {k: v for k, v in m.items() if k in ("role", "content")}
            if m.get("attachments"):
                # Resolve each attachment's CURRENT state (deleted or downloadable) at read time.
                entry["attachments"] = chat_attachments.resolve_message_attachments(ctx, m["attachments"])
            out_messages.append(entry)
        session["messages"] = out_messages
        return session

    @api.post("/api/sessions/{session_id}/distill", tags=["Sessions & projects"], summary="Extract durable facts from a conversation into searchable memory.")
    def distill_session(session_id: str, authorization: str | None = Header(default=None)):
        _require("sessions:write", _user(authorization))
        try:
            facts = manager.distill(session_id, provider=ctx.build_provider())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"session": session_id, "facts_learned": facts}

    @api.delete("/api/sessions/{session_id}", tags=["Sessions & projects"], summary="Delete one chat session.")
    def delete_session(session_id: str, authorization: str | None = Header(default=None)):
        _require("sessions:write", _user(authorization))
        if not ctx.catalog.get_session(session_id):
            raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
        ctx.catalog.delete_session(session_id)
        return {"deleted": session_id}

    @api.delete("/api/sessions", tags=["Sessions & projects"], summary="Delete all chat sessions, optionally scoped to a project.")
    def delete_sessions(project: str | None = None, authorization: str | None = Header(default=None)):
        """Delete all conversations, optionally scoped to a project."""
        _require("sessions:write", _user(authorization))
        project_row = manager.resolve_project(project)
        if project and not project_row:
            raise HTTPException(status_code=404, detail=f"No project {project!r}")
        deleted = ctx.catalog.delete_sessions(project_row["id"] if project_row else None)
        return {"deleted": deleted}

    @api.post("/api/chat/attachments", tags=["Ask & search"], summary="Upload file(s) as per-question context for a chat message (extracted to text, injected into that turn only). NOT ingested into memory or the Uploads connector; auto-deleted after the retention window.")
    async def upload_chat_attachments(
        files: list[UploadFile] = File(...), authorization: str | None = Header(default=None)
    ):
        """Attach documents to a question. Each file's text is extracted and stored as
        short-lived context (see `chat.context_retention_days`) — separate from learned memory.
        Returns metadata (id/filename/size/…) to send back as `attachment_ids` on POST /api/chat."""
        from quickjoiner import chat_attachments

        user = _user(authorization)
        _require_user(user)
        _require("chat:use", user)
        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")
        out = []
        for f in files:
            data = await f.read()
            if not data:
                continue
            # store_attachment is synchronous — extracting a multi-MB office doc or expanding a
            # zip archive can take several seconds of real CPU/IO work, and an `async def`
            # handler calling it directly blocks the whole event loop for that long (every other
            # request and SSE stream on the server stalls too, not just this one). Reported live
            # as "the system freezes for a few seconds" on a 4-5MB attachment — run_in_threadpool
            # is what actually frees the loop up while it runs.
            out.append(await run_in_threadpool(
                chat_attachments.store_attachment, ctx, f.filename or "attachment", data, f.content_type or ""))
        if not out:
            raise HTTPException(status_code=400, detail="No non-empty files uploaded")
        return {"attachments": out}

    @api.get("/api/chat/attachments/{att_id}/download", tags=["Ask & search"], summary="Download a chat context file. Returns 410 Gone once it has been auto-deleted by the retention sweep.")
    def download_chat_attachment(att_id: str, authorization: str | None = Header(default=None)):
        """Serve the original attached file. 404 if unknown; 410 Gone once the retention sweep
        has deleted the bytes (the chat history still shows the name + when it went)."""
        from fastapi.responses import FileResponse

        from quickjoiner import chat_attachments

        user = _user(authorization)
        _require_user(user)
        _require("chat:use", user)
        row = ctx.catalog.get_context_attachment(att_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such attachment")
        path = chat_attachments.attachment_original_path(ctx, att_id)
        if path is None:
            raise HTTPException(
                status_code=410,
                detail=f"This file was deleted on {row.get('deleted_at') or 'expiry'} "
                       f"({ctx.config.chat.context_retention_days}-day retention).")
        return FileResponse(path, filename=row["filename"],
                            media_type=row.get("content_type") or "application/octet-stream")

    @api.post("/api/chat", tags=["Ask & search"], summary="Ask a grounded, cited question. Streams Server-Sent Events: thinking / delta / tool_call / candidates / answer / done. Answers only from learned memory, or says it hasn't learned that yet.")
    def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
        """SSE stream: {type: thinking|delta|tool_call|candidates|answer|error|done, data: ...}
        events. `candidates` (plan 06 §C) carries a JSON list of validated multi-angle
        candidate answers, emitted before `answer` when the model produced a valid
        ```candidates block; confidence values in it are server-computed."""
        events: queue.Queue = queue.Queue()
        user = _user(authorization)

        def worker():
            try:
                from quickjoiner import chat_attachments

                session = manager.open_session(req.session_id, req.project)
                history = manager.history(session)
                # Per-question attachments (context for THIS turn only, never memory): their text
                # rides the system prompt, their metadata is stamped on the user message.
                att_block, att_meta = chat_attachments.build_context_block(ctx, req.attachment_ids)
                # Resolve the picked scope (connectors / documents / tags) into the concrete
                # ids the stores filter on. Done once, server-side, before any search — the
                # model is never asked to work out what "the Zix deck" means.
                search_scope, scope_note = _resolve_scope(req.scope)
                extra = "\n\n".join(
                    s for s in (manager.system_context(session), att_block, scope_note) if s)
                # Built inside the worker so provider setup errors (e.g. missing
                # ANTHROPIC_API_KEY) surface as SSE error events, not a 500.
                # Live connector tools are scoped to sources this user may see.
                agent = ctx.build_agent(
                    req.provider, req.model, extra_system=extra or None,
                    sources=ctx.visible_sources(user), user=user, scope=search_scope,
                )
                turn_index = len(history)
                answer, new_history = agent.ask(
                    req.message,
                    history,
                    on_event=lambda etype, detail: events.put({"type": etype, "data": detail}),
                )
                # Stamp the attachment metadata onto this turn's user message so reloaded
                # history renders the chips beneath the question, and bind them to the session.
                if att_meta and turn_index < len(new_history) and new_history[turn_index].get("role") == "user":
                    new_history[turn_index]["attachments"] = att_meta
                    ctx.catalog.link_context_attachments(req.attachment_ids, session["id"])
                manager.record_turn(session["id"], new_history)
                manager.maybe_compress(session["id"])
                events.put({"type": "answer", "data": answer, "session_id": session["id"]})
            except Exception as exc:
                events.put({"type": "error", "data": str(exc)})
            finally:
                events.put(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()

        def stream():
            while True:
                item = events.get()
                if item is _SENTINEL:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                yield f"data: {json.dumps(item)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return api
