"""FastAPI app: chat (SSE), sources dashboard, sync, search, webhooks."""

from __future__ import annotations

import json
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
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


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    project: str | None = None  # project id or name; groups sessions + scopes memory
    provider: str | None = None
    model: str | None = None


class ProjectRequest(BaseModel):
    name: str
    description: str = ""


class CredentialsRequest(BaseModel):
    username: str
    password: str


class LearnRequest(BaseModel):
    fact: str
    topic: str | None = None


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


def create_app(workspace: Path) -> FastAPI:
    from quickjoiner.sessions import SessionManager

    from quickjoiner.suggest import QuestionSuggester
    from quickjoiner.sync_manager import SyncManager, _DONE

    ctx: AppContext = build_context(workspace)
    api = FastAPI(
        title="QuickJoiner API",
        version="0.1.0",
        description=_API_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
    )
    manager = SessionManager(ctx)
    auth = Auth(ctx.catalog)
    suggester = QuestionSuggester(ctx.catalog)
    syncs = SyncManager(ctx)
    api.include_router(build_hooks_router(ctx))

    def _user(authorization: str | None) -> str | None:
        """Bearer token -> username; None in open mode or when signed out."""
        if authorization and authorization.lower().startswith("bearer "):
            return auth.resolve(authorization[7:].strip())
        return None

    def _require_user(user: str | None) -> None:
        if auth.enabled and user is None:
            raise HTTPException(status_code=401, detail="Sign in to manage connectors")

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
    @api.get("/api/auth/status", tags=["Authentication"], summary="Whether sign-in is enabled and who (if anyone) the bearer token identifies.")
    def auth_status(authorization: str | None = Header(default=None)):
        return {"enabled": auth.enabled, "user": _user(authorization)}

    @api.post("/api/auth/users", tags=["Authentication"], summary="Create a user. The FIRST user turns authentication ON for the workspace (open mode until then).")
    def create_user(req: CredentialsRequest, authorization: str | None = Header(default=None)):
        # Bootstrap: anyone may create the FIRST user; after that, sign-in required.
        _require_user(_user(authorization))
        try:
            auth.create_user(req.username, req.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"username": req.username.strip()}

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

    @api.patch("/api/connectors/{name}", tags=["Connectors"], summary="Update a connector's options, sharing, or sync schedule. Secrets sent back as the mask are preserved; a field cleared to empty is removed.")
    def update_connector(
        name: str, req: ConnectorUpdate, authorization: str | None = Header(default=None)
    ):
        user = _user(authorization)
        _require_user(user)
        source = _find_source(name, user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can change this connector")
        if req.shared is not None:
            source.shared = req.shared
        if req.clear_sync_interval:
            source.sync_interval_minutes = None
        elif req.sync_interval_minutes is not None:
            source.sync_interval_minutes = req.sync_interval_minutes
        if req.options is not None:
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
        source = _find_source(name, user)
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
        user = _user(authorization)
        _require_user(user)
        source = _find_source(name, user)
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
        if keep_memory:
            ctx.catalog.delete_source(source_id)
            return {"removed": name, "job": None}
        # The job drops the catalog row itself, after the documents/vectors/graph are gone.
        job = syncs.start_cleanup(name, source_id)
        return {"removed": name, "job": job.summary()}

    @api.post("/api/connectors/{name}/test", tags=["Connectors"], summary="Test a connector's credentials / reachability without ingesting anything.")
    def test_connector(name: str, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors.registry import create_connector

        user = _user(authorization)
        source = _find_source(name, user)
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
        _require_user(_user(authorization))
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
        return _settings_view()

    @api.post("/api/llm/test", tags=["Settings"], summary="Probe the configured LLM provider with a one-token round-trip. Accepts UNSAVED llm overrides so a client can verify a proxy/model before saving.")
    def test_llm(req: LLMTestRequest, authorization: str | None = Header(default=None)):
        """Probe the configured LLM provider with a one-token round-trip. Accepts
        optional `llm` overrides so the settings form can test UNSAVED values (proxy
        URL, model, api-key env var) before persisting. Never saves."""
        _require_user(_user(authorization))
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

    @api.post("/api/sync/{source_name}", tags=["Sync & ingestion"], summary="Start a background sync job for a source. clean=true purges the source first for a from-scratch re-pull. Returns immediately with the job.")
    def sync_source(source_name: str, clean: bool = False,
                    authorization: str | None = Header(default=None)):
        """Start a background sync job (returns immediately). `clean=true` purges the
        source's documents/vectors/graph first for a from-scratch, non-corrupted resync.
        Multiple different sources can sync at once; watch progress on the logs stream."""
        _find_source(source_name, _user(authorization))  # visibility + existence gate
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
        _find_source(source_name, _user(authorization))
        try:
            job = syncs.stop(source_name, cleanup=cleanup)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"job": job.summary()}

    @api.post("/api/sync/{source_name}/pause", tags=["Sync & ingestion"], summary="Pause a running sync in place (holds both the pull and the graph-extraction tail). The same in-memory run continues on resume — nothing re-pulls.")
    def pause_sync(source_name: str, authorization: str | None = Header(default=None)):
        """Hold a running sync in place (after the current document / between graph
        batches). Committed work stays; resume continues the same in-memory run."""
        _find_source(source_name, _user(authorization))
        try:
            job = syncs.pause(source_name)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job": job.summary()}

    @api.post("/api/sync/{source_name}/resume", tags=["Sync & ingestion"], summary="Resume a paused sync.")
    def resume_sync(source_name: str, authorization: str | None = Header(default=None)):
        """Resume a paused sync."""
        _find_source(source_name, _user(authorization))
        try:
            job = syncs.resume(source_name)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job": job.summary()}

    @api.get("/api/syncs", tags=["Sync & ingestion"], summary="Live state of every sync job in this server process (running + finished this session), including current stage and estimated %.")
    def list_syncs(authorization: str | None = Header(default=None)):
        """State of every sync job (running + finished this session)."""
        return {"syncs": syncs.status()}

    @api.post("/api/memory/reset", tags=["Sync & ingestion"], summary="DANGER: wipe ALL ingested knowledge — documents, vectors, the whole knowledge graph, and every sync watermark — leaving the workspace as if nothing had synced. KEEPS connectors, users, chat, and settings. Refuses while any sync is running.")
    def reset_memory(authorization: str | None = Header(default=None)):
        """Wipe ALL ingested knowledge — documents, vectors, FTS, the whole knowledge
        graph, and every sync watermark — leaving the workspace as if nothing had synced.
        KEEPS connector configs, users, chat sessions/projects, and settings; the next
        sync of each connector is a full pull. Refuses (409) while any sync/cleanup job is
        active, since it clears every source at once."""
        _require_user(_user(authorization))
        active = [j for j in syncs.status() if j["state"] in ("running", "paused", "stopping")]
        if active:
            names = ", ".join(sorted(j["source"] for j in active))
            raise HTTPException(status_code=409,
                                detail=f"A sync is running ({names}) — stop it before resetting memory")
        counts = ctx.catalog.reset_knowledge()
        ctx.store.reset()
        return {"reset": True, "removed": counts}

    @api.get("/api/notifications", tags=["Sync & ingestion"], summary="Sync activity feed over the last N hours (default 24) — running jobs with live state plus finished ones from persisted history. Survives a server restart.")
    def notifications(hours: int = 24, authorization: str | None = Header(default=None)):
        """Sync activity over the last `hours` (default 24), newest first — running jobs
        with live state plus finished ones from the persisted history. Backs the
        notification menu; read-state is tracked client-side."""
        _user(authorization)
        items = syncs.recent(hours=max(1, min(hours, 24 * 7)))
        active = sum(1 for i in items if i["state"] in ("running", "stopping"))
        return {"notifications": items, "active": active}

    @api.get("/api/sync/{source_name}/logs", tags=["Sync & ingestion"], summary="Server-Sent Events stream of a sync's live log lines (replays the backlog first), ending with a terminal `done` event.")
    def sync_logs(source_name: str, authorization: str | None = Header(default=None)):
        """SSE stream of a sync's live log lines (replays the backlog first), then a
        terminal `done` event carrying the job's final state."""
        _find_source(source_name, _user(authorization))
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

        _require_user(_user(authorization))
        fact = req.fact.strip()
        if not fact:
            raise HTTPException(status_code=400, detail="Nothing to learn: empty fact")
        return {"result": teach_fact(ctx.catalog, ctx.pipeline, fact, req.topic)}

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
        _require_user(_user(authorization))
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

        _require_user(_user(authorization))
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
    def make_brief(brief_type: str, provider: str | None = None, model: str | None = None):
        from quickjoiner.agent.briefs import generate_brief

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
    def make_agents_md(source_name: str, provider: str | None = None, model: str | None = None):
        from quickjoiner.agent.repo_docs import generate_agents_md

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
    def create_project(req: ProjectRequest):
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
        session["messages"] = [
            {k: v for k, v in m.items() if k in ("role", "content")}
            for m in json.loads(session.pop("messages_json"))
        ]
        return session

    @api.post("/api/sessions/{session_id}/distill", tags=["Sessions & projects"], summary="Extract durable facts from a conversation into searchable memory.")
    def distill_session(session_id: str):
        try:
            facts = manager.distill(session_id, provider=ctx.build_provider())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"session": session_id, "facts_learned": facts}

    @api.delete("/api/sessions/{session_id}", tags=["Sessions & projects"], summary="Delete one chat session.")
    def delete_session(session_id: str):
        if not ctx.catalog.get_session(session_id):
            raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
        ctx.catalog.delete_session(session_id)
        return {"deleted": session_id}

    @api.delete("/api/sessions", tags=["Sessions & projects"], summary="Delete all chat sessions, optionally scoped to a project.")
    def delete_sessions(project: str | None = None):
        """Delete all conversations, optionally scoped to a project."""
        project_row = manager.resolve_project(project)
        if project and not project_row:
            raise HTTPException(status_code=404, detail=f"No project {project!r}")
        deleted = ctx.catalog.delete_sessions(project_row["id"] if project_row else None)
        return {"deleted": deleted}

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
                session = manager.open_session(req.session_id, req.project)
                history = manager.history(session)
                # Built inside the worker so provider setup errors (e.g. missing
                # ANTHROPIC_API_KEY) surface as SSE error events, not a 500.
                # Live connector tools are scoped to sources this user may see.
                agent = ctx.build_agent(
                    req.provider, req.model, extra_system=manager.system_context(session),
                    sources=ctx.visible_sources(user),
                )
                answer, new_history = agent.ask(
                    req.message,
                    history,
                    on_event=lambda etype, detail: events.put({"type": etype, "data": detail}),
                )
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
