"""FastAPI app: chat (SSE), sources dashboard, sync, search, webhooks."""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from quickjoiner.api.hooks import build_hooks_router
from quickjoiner.app import AppContext, build_context
from quickjoiner.auth import Auth, can_manage, visible
from quickjoiner.config import SourceConfig, save_config

STATIC_DIR = Path(__file__).parent / "static"

_SENTINEL = object()
MASKED = "•••"


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


class ConnectorRequest(BaseModel):
    name: str
    type: str
    options: dict = {}
    shared: bool = False
    skip_test: bool = False


class ConnectorUpdate(BaseModel):
    options: dict | None = None
    shared: bool | None = None


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

    ctx: AppContext = build_context(workspace)
    api = FastAPI(title="QuickJoiner", version="0.1.0")
    manager = SessionManager(ctx)
    auth = Auth(ctx.catalog)
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
            "options": _mask_options(source.type, source.options),
            "modes": modes,
        }

    # -- auth -----------------------------------------------------------------
    @api.get("/api/auth/status")
    def auth_status(authorization: str | None = Header(default=None)):
        return {"enabled": auth.enabled, "user": _user(authorization)}

    @api.post("/api/auth/users")
    def create_user(req: CredentialsRequest, authorization: str | None = Header(default=None)):
        # Bootstrap: anyone may create the FIRST user; after that, sign-in required.
        _require_user(_user(authorization))
        try:
            auth.create_user(req.username, req.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"username": req.username.strip()}

    @api.post("/api/auth/login")
    def login(req: CredentialsRequest):
        try:
            token = auth.login(req.username, req.password)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        return {"token": token, "username": req.username}

    @api.post("/api/auth/logout")
    def logout(authorization: str | None = Header(default=None)):
        if authorization and authorization.lower().startswith("bearer "):
            auth.logout(authorization[7:].strip())
        return {"ok": True}

    # -- connector management ---------------------------------------------------
    @api.get("/api/connectors/types")
    def connector_types():
        from quickjoiner.connectors.specs import connector_catalog

        return connector_catalog()

    @api.get("/api/connectors")
    def list_connectors(authorization: str | None = Header(default=None)):
        user = _user(authorization)
        return [
            _connector_row(s, user)
            for s in ctx.config.sources
            if visible(s, user, auth.enabled)
        ]

    @api.post("/api/connectors")
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
        save_config(ctx.workspace, ctx.config)
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
        return {**_connector_row(source, user), "test": test_result}

    @api.patch("/api/connectors/{name}")
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
        save_config(ctx.workspace, ctx.config)
        ctx.catalog.upsert_source(
            f"{source.type}:{source.name}", source.name, source.type, source.options
        )
        return _connector_row(source, user)

    @api.delete("/api/connectors/{name}")
    def delete_connector(name: str, authorization: str | None = Header(default=None)):
        user = _user(authorization)
        _require_user(user)
        source = _find_source(name, user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can remove this connector")
        ctx.config.sources = [s for s in ctx.config.sources if s.name != name]
        save_config(ctx.workspace, ctx.config)
        ctx.catalog.delete_source(f"{source.type}:{source.name}")
        return {"removed": name}

    @api.post("/api/connectors/{name}/test")
    def test_connector(name: str, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors.registry import create_connector

        user = _user(authorization)
        source = _find_source(name, user)
        result = create_connector(source, ctx.workspace).test()
        return {"ok": result.ok, "message": result.message}

    @api.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @api.get("/health")
    def health():
        return {"status": "ok", "workspace": str(ctx.workspace), "org": ctx.config.org}

    @api.get("/api/status")
    def status():
        return {
            "org": ctx.config.org,
            "llm": {"provider": ctx.config.llm.provider, "model": ctx.config.llm.resolved_model()},
            "stats": ctx.catalog.stats(),
        }

    @api.get("/api/sources")
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

    @api.post("/api/sync/{source_name}")
    def sync_source(source_name: str, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors.registry import create_connector

        source = _find_source(source_name, _user(authorization))
        connector = create_connector(source, ctx.workspace)
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
        state = ctx.catalog.get_sync_state(connector.source_id)
        started = datetime.now(timezone.utc).isoformat()
        stats = ctx.pipeline.ingest(connector.sync(state), connector.source_id)
        ctx.catalog.set_sync_state(connector.source_id, "since", started)
        return {"source": source_name, "result": stats.summary(), "errors": stats.errors[:10]}

    @api.get("/api/search")
    def search(q: str, top_k: int = 8):
        hits = ctx.store.search(q, top_k=top_k, min_score=ctx.config.retrieval.min_score)
        return [
            {"score": round(h.score, 3), "title": h.title, "uri": h.uri, "kind": h.kind,
             "text": h.text[:500]}
            for h in hits
        ]

    @api.get("/api/briefs")
    def list_briefs():
        briefs_dir = ctx.workspace / "briefs"
        files = sorted(briefs_dir.glob("*.md")) if briefs_dir.exists() else []
        return [{"name": f.stem, "path": str(f)} for f in files]

    @api.post("/api/briefs/{brief_type}")
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

    @api.get("/api/projects")
    def list_projects():
        return ctx.catalog.list_projects()

    @api.post("/api/projects")
    def create_project(req: ProjectRequest):
        return manager.create_project(req.name, req.description)

    @api.get("/api/sessions")
    def list_sessions(project: str | None = None):
        project_row = manager.resolve_project(project)
        if project and not project_row:
            raise HTTPException(status_code=404, detail=f"No project {project!r}")
        return ctx.catalog.list_sessions(project_row["id"] if project_row else None)

    @api.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        session = ctx.catalog.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
        session["messages"] = [
            {k: v for k, v in m.items() if k in ("role", "content")}
            for m in json.loads(session.pop("messages_json"))
        ]
        return session

    @api.post("/api/sessions/{session_id}/distill")
    def distill_session(session_id: str):
        try:
            facts = manager.distill(session_id, provider=ctx.build_provider())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"session": session_id, "facts_learned": facts}

    @api.post("/api/chat")
    def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
        """SSE stream: {type: thinking|delta|tool_call|answer|error|done, data: ...} events."""
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
