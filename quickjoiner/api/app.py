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
    api = FastAPI(title="QuickJoiner", version="0.1.0")
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

    @api.delete("/api/connectors/{name}")
    def delete_connector(name: str, authorization: str | None = Header(default=None)):
        user = _user(authorization)
        _require_user(user)
        source = _find_source(name, user)
        if not can_manage(source, user, auth.enabled):
            raise HTTPException(status_code=403, detail="Only the owner can remove this connector")
        ctx.config.sources = [s for s in ctx.config.sources if s.name != name]
        ctx.catalog.save_config(ctx.config)  # reconciles: removes this source's row
        ctx.catalog.delete_source(f"{source.type}:{source.name}")
        return {"removed": name}

    @api.post("/api/connectors/{name}/test")
    def test_connector(name: str, authorization: str | None = Header(default=None)):
        from quickjoiner.connectors.registry import create_connector

        user = _user(authorization)
        source = _find_source(name, user)
        result = create_connector(source, ctx.workspace).test()
        return {"ok": result.ok, "message": result.message}

    ui_dir = _ui_dir()
    if (ui_dir / "assets").is_dir():  # React build: hashed js/css bundles
        api.mount("/assets", StaticFiles(directory=ui_dir / "assets"), name="assets")

    @api.get("/", response_class=HTMLResponse)
    def index():
        return (ui_dir / "index.html").read_text(encoding="utf-8")

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

    @api.get("/api/settings")
    def get_settings():
        return _settings_view()

    @api.patch("/api/settings")
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

    @api.post("/api/llm/test")
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

    @api.post("/api/sync/{source_name}/stop")
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

    @api.get("/api/syncs")
    def list_syncs(authorization: str | None = Header(default=None)):
        """State of every sync job (running + finished this session)."""
        return {"syncs": syncs.status()}

    @api.get("/api/sync/{source_name}/logs")
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

    @api.post("/api/learn")
    def learn(req: LearnRequest, authorization: str | None = Header(default=None)):
        """Teach qj a free-text fact from the UI — same store as the agent's
        `remember` tool and `qj learn "<text>"`, no LLM round-trip needed."""
        from quickjoiner.agent.tools import teach_fact

        _require_user(_user(authorization))
        fact = req.fact.strip()
        if not fact:
            raise HTTPException(status_code=400, detail="Nothing to learn: empty fact")
        return {"result": teach_fact(ctx.catalog, ctx.pipeline, fact, req.topic)}

    @api.get("/api/gaps")
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

    @api.post("/api/gaps/resolve")
    def resolve_gaps(req: GapsResolveRequest, authorization: str | None = Header(default=None)):
        """Mark gaps resolved (after connecting a source, teaching, or dismissing)."""
        _require_user(_user(authorization))
        ctx.catalog.resolve_gaps(req.gap_ids, req.resolution or "dismissed")
        return {"resolved": len(req.gap_ids)}

    @api.get("/api/suggest")
    def suggest(q: str = "", limit: int = 6):
        """Question autocomplete as the user types — keyless/deterministic, drawn
        from the knowledge graph (entity-templated questions), past questions, and
        source-aware starters. Fast enough for per-keystroke use (no LLM)."""
        return {"suggestions": suggester.suggest(q, limit=max(1, min(limit, 10)))}

    @api.post("/api/scrape")
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

    @api.get("/api/graph/path")
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

    @api.get("/api/graph")
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

    @api.get("/api/graph/search")
    def graph_search(q: str, limit: int = 10):
        """Entity autocomplete for the graph view's search box — substring match
        over names/aliases, not the exact resolve /api/graph does."""
        return ctx.catalog.search_entities(q, limit)

    @api.get("/api/graph/bridges")
    def graph_bridges(limit: int = 20):
        """Entities touched by more than one source's edges — cross-source
        correlation, and a much better "where do I start?" list than a slice of
        the raw graph."""
        return ctx.catalog.bridge_entities(limit)

    @api.get("/api/documents/{doc_id}/file")
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

    @api.post("/api/repos/{source_name}/agents-md")
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

    @api.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str):
        if not ctx.catalog.get_session(session_id):
            raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
        ctx.catalog.delete_session(session_id)
        return {"deleted": session_id}

    @api.delete("/api/sessions")
    def delete_sessions(project: str | None = None):
        """Delete all conversations, optionally scoped to a project."""
        project_row = manager.resolve_project(project)
        if project and not project_row:
            raise HTTPException(status_code=404, detail=f"No project {project!r}")
        deleted = ctx.catalog.delete_sessions(project_row["id"] if project_row else None)
        return {"deleted": deleted}

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
